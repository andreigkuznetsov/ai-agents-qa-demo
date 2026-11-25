import asyncio
import hashlib
import multiprocessing
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from conftest import Project
from kt4_executor_support import make_private_journal_dir
from starlette.types import ASGIApp, Receive, Scope, Send
from test_kt3_fixtures import CLIENT_A, CLIENT_B, create_run, initialize
from test_kt4_executor_core import TEXT
from test_kt4_tool_adapter_integration import case_ids, serve_loopback

from app.logistics_agent_events import AttemptEvent, EvidenceContext, Phase
from app.logistics_agent_executor import OperationExecutor
from app.logistics_agent_journal import (
    OperationJournal,
)
from app.logistics_agent_tools import TrustedContext
from app.logistics_auth import LogisticsContext
from app.logistics_faults import FaultPhase
from app.main import create_app


@dataclass
class ObserveHTTP:
    app: ASGIApp
    # These synthetic values stay in memory and must never appear in assertions.
    calls: list[tuple[str, str, bytes | None, bytes | None]] = field(
        default_factory=list, repr=False
    )
    revoke_check: bool = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            self.calls.append(
                (
                    scope["method"],
                    scope["path"],
                    headers.get(b"idempotency-key"),
                    headers.get(b"authorization"),
                )
            )
            if self.revoke_check and scope["path"].endswith("/case-result"):
                scope = {
                    **scope,
                    "headers": [
                        (k, v) for k, v in scope["headers"] if k != b"authorization"
                    ],
                }
        await self.app(scope, receive, send)

    def counts(self) -> tuple[int, int]:
        return (
            sum(method == "POST" for method, *_ in self.calls),
            sum(path.endswith("/case-result") for _, path, *_ in self.calls),
        )

    def check_context(self) -> None:
        scoped = [
            (key, auth)
            for method, path, key, auth in self.calls
            if method == "POST" or path.endswith("/case-result")
        ]
        if len(scoped) != 2 or scoped[0] != scoped[1] or not all(scoped[0]):
            pytest.fail(
                "POST and check did not share the original trusted context",
                pytrace=False,
            )


def new_fixture(project: Project) -> UUID:
    run = create_run(project)
    assert initialize(project, run)
    return run


class PauseAtPermit:
    def __init__(self, channel: Connection) -> None:
        self.channel = channel

    def emit(self, event: AttemptEvent) -> None:
        if event.phase is Phase.PERMIT_CONSUMED:
            self.channel.send("PERMIT_DURABLE")
            self.channel.recv()


def process_operation(
    path: Path,
    operation: UUID,
    context: TrustedContext,
    connection: Connection,
    pause: bool,
) -> None:
    evidence = (
        (PauseAtPermit(connection), EvidenceContext(uuid4(), "a" * 40))
        if pause
        else None
    )
    try:
        result = asyncio.run(
            OperationExecutor(OperationJournal(path), 12, evidence).execute(
                operation, context, TEXT
            )
        )
        connection.send(result.outcome)
    finally:
        connection.close()


class Receiver(Protocol):
    def poll(self, timeout: float, /) -> bool: ...
    def recv(self) -> object: ...


async def child_message(channel: Receiver) -> str:
    # PipeConnection and Connection both have poll, recv and close on their platforms.
    assert await asyncio.to_thread(channel.poll, 8)
    result = channel.recv()
    assert isinstance(result, str)
    return result


def test_crash_and_concurrent_delivery_over_tcp(
    migrated_db: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = make_private_journal_dir(tmp_path)
    migrated_db.api_env(monkeypatch)
    modes = ("before-wire", "pre-commit", "post-commit")
    runs = {mode: new_fixture(migrated_db) for mode in modes}
    owners = {mode: LogisticsContext(run, CLIENT_B) for mode, run in runs.items()}
    tokens = {mode: uuid4().hex for mode in modes}
    auth = {
        hashlib.sha256(tokens[mode].encode()).hexdigest(): owners[mode]
        for mode in modes
    }
    auth_token_a = uuid4().hex
    auth[hashlib.sha256(auth_token_a.encode()).hexdigest()] = LogisticsContext(
        runs["post-commit"], CLIENT_A
    )

    async def exercise() -> None:
        reached = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()

        async def hook(context: LogisticsContext, phase: FaultPhase) -> None:
            if (
                context == owners["pre-commit"]
                and phase == "before_commit"
                or context == owners["post-commit"]
                and phase == "after_commit"
            ):
                reached.set()
                try:
                    await release.wait()
                    if phase == "before_commit":
                        raise RuntimeError("synthetic rollback after worker stop")
                finally:
                    cancelled.set()

        app = ObserveHTTP(create_app(auth, case_fault=hook, case_write_timeout=15))
        processes = multiprocessing.get_context("spawn")
        async with serve_loopback(app) as origin:
            for mode, run in runs.items():
                context = TrustedContext(run, CLIENT_B, tokens[mode], origin, 2026)
                operation = uuid4()
                journal = OperationJournal(root)
                journal.prepare(operation, context)
                app.calls.clear()
                reached.clear()
                release.clear()
                cancelled.clear()
                parent, child = processes.Pipe()
                worker = processes.Process(
                    target=process_operation,
                    args=(root, operation, context, child, mode == "before-wire"),
                )
                worker.start()
                child.close()
                try:
                    if mode == "before-wire":
                        assert await child_message(parent) == "PERMIT_DURABLE"
                        assert app.counts() == (0, 0)
                    else:
                        await asyncio.wait_for(reached.wait(), timeout=10)
                        assert app.counts() == (1, 0)

                    if mode == "post-commit":
                        second, sender = processes.Pipe()
                        competitor = processes.Process(
                            target=process_operation,
                            args=(root, operation, context, sender, False),
                        )
                        competitor.start()
                        sender.close()
                        try:
                            assert await child_message(second) == "BUSY"
                        finally:
                            await asyncio.to_thread(competitor.join, 3)
                            if competitor.is_alive():
                                competitor.kill()
                                await asyncio.to_thread(competitor.join, 3)
                            second.close()
                        assert competitor.exitcode == 0
                        assert app.counts() == (1, 0)
                    worker.kill()
                    await asyncio.to_thread(worker.join, 5)
                    assert worker.exitcode not in (0, None)
                finally:
                    release.set()
                    if worker.is_alive():
                        worker.kill()
                        await asyncio.to_thread(worker.join, 5)
                    parent.close()
                if mode != "before-wire":
                    await asyncio.wait_for(cancelled.wait(), timeout=5)
                with journal.ownership(operation, context) as owned:
                    assert owned is not None
                    state = owned.read()
                    assert (
                        state.consumed
                        and not state.handed_off
                        and not state.checked
                        and state.case_id is None
                    )
                before = len(app.calls)
                result = await OperationExecutor(journal, 2).execute(
                    operation, context, TEXT
                )
                assert result.outcome == "UNKNOWN" and result.case_id is None
                if mode == "post-commit":
                    wrong = TrustedContext(run, CLIENT_A, auth_token_a, origin, 2026)
                    result = await OperationExecutor(journal, 2).execute(
                        operation, wrong, TEXT
                    )
                    assert result.outcome == "UNKNOWN" and result.case_id is None
                assert len(app.calls) == before
                assert len(case_ids(migrated_db, run, CLIENT_B)) == int(
                    mode == "post-commit"
                )
                with migrated_db.db() as observer:
                    row = observer.execute(
                        "SELECT count(*) FROM qa.las_idempotency_keys WHERE run_id=%s",
                        (run,),
                    ).fetchone()
                assert row == (int(mode == "post-commit"),)

    asyncio.run(exercise())
