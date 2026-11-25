import asyncio
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import Project
from kt4_executor_support import make_private_journal_dir
from test_kt3_fixtures import CLIENT_B, SAME_ORDER
from test_kt4_executor_core import DAY, TEXT
from test_kt4_executor_recovery import install_abort
from test_kt4_executor_recovery_integration import ObserveHTTP, new_fixture
from test_kt4_tool_adapter_integration import case_ids

from app.logistics_agent_events import (
    AttemptEvent,
    EvidenceContext,
    JsonlEventSink,
    Phase,
)
from app.logistics_agent_executor import OperationExecutor
from app.logistics_agent_journal import (
    JournalUnavailable,
    OperationJournal,
    OwnedOperation,
)
from app.logistics_agent_tools import CreateCaseResult, ToolAdapter, TrustedContext
from app.logistics_auth import LogisticsContext
from app.logistics_faults import FaultPhase, ResultCheckUnavailable
from app.main import create_app


def _life(run: UUID, token: str, origin: str) -> TrustedContext:
    return TrustedContext(run, CLIENT_B, token, origin, 2026)


class DropPostResponse(httpx2.AsyncBaseTransport):
    def __init__(self, wire: ObserveHTTP, held: asyncio.Event | None = None) -> None:
        self.api = httpx2.ASGITransport(app=wire, raise_app_exceptions=False)
        self.held = held
        self.pending: asyncio.Task[httpx2.Response] | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return await self.api.handle_async_request(request)
        if self.held is None:
            response = await self.api.handle_async_request(request)
            assert response.status_code == 201
        else:
            self.pending = asyncio.create_task(self.api.handle_async_request(request))
            await asyncio.wait_for(self.held.wait(), timeout=5)
        raise httpx2.ReadError("synthetic response loss")


def test_reconciliation_with_postgres_visibility_and_failures(
    migrated_db: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = OperationJournal(make_private_journal_dir(tmp_path))
    migrated_db.api_env(monkeypatch)
    modes = ("found", "rollback", "uncommitted", "503", "401", "deadline")
    runs = {mode: new_fixture(migrated_db) for mode in modes}
    tokens = {mode: uuid4().hex for mode in modes}
    auth = {
        hashlib.sha256(tokens[mode].encode()).hexdigest(): LogisticsContext(
            run, CLIENT_B
        )
        for mode, run in runs.items()
    }

    async def exercise() -> None:
        held, release = asyncio.Event(), asyncio.Event()

        async def faults(context: LogisticsContext, phase: FaultPhase) -> None:
            if phase == "before_commit" and context.run_id in {
                runs["rollback"],
                runs["uncommitted"],
            }:
                held.set()
                await release.wait()
                if context.run_id == runs["rollback"]:
                    raise RuntimeError("synthetic rollback")
            if phase == "before_result_read":
                if context.run_id == runs["503"]:
                    raise ResultCheckUnavailable
                if context.run_id == runs["deadline"]:
                    await asyncio.sleep(3)

        simulator = create_app(
            auth, case_fault=faults, case_write_timeout=12, case_result_timeout=5
        )
        async with simulator.router.lifespan_context(simulator):
            for mode, run in runs.items():
                held.clear()
                release.clear()
                context = _life(run, tokens[mode], "http://synthetic-simulator.test")
                key = uuid4()
                journal.prepare(key, context)
                wire = ObserveHTTP(simulator, revoke_check=mode == "401")
                executor = OperationExecutor(journal, 1.5)
                transport = DropPostResponse(
                    wire, held if mode in {"rollback", "uncommitted"} else None
                )
                try:
                    first = await executor.execute(
                        key, context, TEXT, test_transport=transport
                    )
                    assert wire.counts() == (1, 1)
                    wire.check_context()
                    snapshot = case_ids(migrated_db, run, CLIENT_B)
                    if mode in {"rollback", "uncommitted"}:
                        assert not snapshot
                        assert (
                            transport.pending is not None
                            and not transport.pending.done()
                        )
                    else:
                        assert len(snapshot) == 1
                    replay = await executor.execute(
                        key, context, TEXT, test_transport=transport
                    )
                    assert wire.counts() == (1, 1)
                    if mode == "found":
                        assert first == replay and first.case_id in snapshot
                        assert first.outcome == "EXISTING"
                    else:
                        assert first.outcome == replay.outcome == "UNKNOWN"
                        assert first.case_id is None and replay.case_id is None
                finally:
                    release.set()
                    if transport.pending is not None:
                        response = await asyncio.wait_for(transport.pending, timeout=8)
                        assert response.status_code == (
                            500 if mode == "rollback" else 201
                        )
                assert len(case_ids(migrated_db, run, CLIENT_B)) == int(
                    mode != "rollback"
                )
                with migrated_db.db() as observer:
                    row = observer.execute(
                        "SELECT count(*) FROM qa.las_idempotency_keys WHERE run_id=%s",
                        (run,),
                    ).fetchone()
                assert row == (int(mode != "rollback"),)

    asyncio.run(exercise())


class FailingSink:
    def __init__(self, stream: io.StringIO, fail: bool) -> None:
        self.jsonl = JsonlEventSink(stream)
        self.fail = fail

    def emit(self, event: AttemptEvent) -> None:
        if self.fail and event.phase is Phase.FINISHED:
            raise OSError("SYNTHETIC_SINK_EXCEPTION")
        self.jsonl.emit(event)


class ForcePostStatus(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        wire: ObserveHTTP,
        simulator: ObserveHTTP,
        context: TrustedContext,
        status: int,
    ) -> None:
        self.api = httpx2.ASGITransport(app=wire)
        self.simulator = simulator
        self.context = context
        self.status = status

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and self.status == 200:
            # A distinct operation wins after the executor has read the empty case list.
            async with ToolAdapter(
                self.context,
                timeout=4,
                test_transport=httpx2.ASGITransport(app=self.simulator),
            ) as contender:
                result = await contender.create_case(
                    {"order_id": SAME_ORDER, "requested_date": DAY.isoformat()},
                    idempotency_key=uuid4().hex,
                )
                assert isinstance(result, CreateCaseResult) and result.status == 201
        response = await self.api.handle_async_request(request)
        if request.method == "POST":
            assert response.status_code == self.status
        return response


def test_confirmed_results_and_double_failure_with_postgres(
    migrated_db: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrated_db.api_env(monkeypatch)
    modes = ("normal", "journal", "event", "both", "ambiguous")
    scopes = [(status, mode) for status in (200, 201) for mode in modes]
    runs = {scope: new_fixture(migrated_db) for scope in scopes}
    tokens = {scope: uuid4().hex for scope in scopes}
    auth = {
        hashlib.sha256(tokens[scope].encode()).hexdigest(): LogisticsContext(
            run, CLIENT_B
        )
        for scope, run in runs.items()
    }
    confirm = OwnedOperation.confirm

    def lose_confirmation(owned: OwnedOperation, case_id: UUID, status: int) -> None:
        confirm(owned, case_id, status)
        raise JournalUnavailable

    async def exercise() -> None:
        simulator = create_app(auth)
        async with simulator.router.lifespan_context(simulator):
            for http_status, mode in scopes:
                run = runs[(http_status, mode)]
                token = tokens[(http_status, mode)]
                context = _life(run, token, "http://synthetic-simulator.test")
                parent = tmp_path / f"{http_status}-{mode}"
                parent.mkdir(mode=0o700)
                path = make_private_journal_dir(parent)
                journal = OperationJournal(path)
                operation = uuid4()
                journal.prepare(operation, context)
                if mode in {"journal", "both"}:
                    install_abort(path, "case_id")

                report = io.StringIO()
                sink = FailingSink(report, mode in {"event", "both"})
                wire = ObserveHTTP(simulator)
                executor = OperationExecutor(
                    journal, 4, (sink, EvidenceContext(uuid4(), "a" * 40))
                )
                transport = ForcePostStatus(
                    wire, ObserveHTTP(simulator), context, http_status
                )
                with monkeypatch.context() as patch:
                    if mode == "ambiguous":
                        patch.setattr(OwnedOperation, "confirm", lose_confirmation)
                    first = await executor.execute(
                        operation, context, TEXT, test_transport=transport
                    )
                assert wire.counts() == (1, 0)
                ids = case_ids(migrated_db, run, CLIENT_B)
                assert len(ids) == 1
                assert first.evidence_incomplete == (mode in {"event", "both"})
                if mode in {"journal", "both", "ambiguous"}:
                    assert first.outcome == "UNKNOWN" and first.case_id is None
                else:
                    assert first.case_id in ids
                    assert first.outcome == (
                        "CREATED" if http_status == 201 else "EXISTING"
                    )

                output = capsys.readouterr().err
                if mode in {"event", "both"}:
                    notice = json.loads(output)
                    assert notice == {
                        "code": "OPERATION_EVIDENCE_INCOMPLETE",
                        "operation_id": str(operation),
                        "journal_available": mode == "event",
                        "event_sink_available": False,
                    }
                    assert '"phase":"FINISHED"' not in report.getvalue()
                else:
                    assert not output
                for marker in [
                    token,
                    "SYNTHETIC_JOURNAL_DETAIL",
                    "SYNTHETIC_SINK_EXCEPTION",
                    TEXT,
                ]:
                    if marker in output + report.getvalue():
                        pytest.fail("Sensitive material in diagnostics", pytrace=False)
                post = next(call for call in wire.calls if call[0] == "POST")
                if post[2] is None or post[2].decode() in output + report.getvalue():
                    pytest.fail(
                        "Private operation key exposed or missing", pytrace=False
                    )

                replay = await OperationExecutor(journal, 4).execute(
                    operation, context, TEXT, test_transport=transport
                )
                assert wire.counts() == (1, 0)
                if mode in {"journal", "both"}:
                    assert replay.outcome == "UNKNOWN" and replay.case_id is None
                else:
                    assert replay.case_id in ids
                    if mode != "ambiguous":
                        assert replay == replace(first, evidence_incomplete=False)

    asyncio.run(exercise())
