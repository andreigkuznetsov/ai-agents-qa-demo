import asyncio
import io
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from kt4_executor_support import private_journal_dir as private_journal_dir
from test_kt4_executor_core import (
    CASE_ID,
    ORDER,
    TEXT,
    case_json,
    context,
    simulator_handler,
)

from app.logistics_agent_events import (
    AttemptEvent,
    EvidenceContext,
    JsonlEventSink,
    unfinished_attempts,
)
from app.logistics_agent_executor import OperationExecutor
from app.logistics_agent_journal import (
    JournalUnavailable,
    OperationJournal,
    OwnedOperation,
    Permit,
)


def install_abort(path: Path, column: str) -> None:
    assert column in {"case_id", "idempotency_key", "result_check_consumed"}
    with closing(sqlite3.connect(path / "operations.sqlite3")) as db:
        db.execute(f"""
            CREATE TRIGGER abort_test_update BEFORE UPDATE OF {column} ON operations
            BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_JOURNAL_DETAIL'); END
        """)


@pytest.mark.parametrize(
    "committed", [False, True], ids=["rollback", "commit-ack-lost"]
)
def test_permit_failure_never_dispatches(
    private_journal_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    trusted, operation = context(), uuid4()
    journal = OperationJournal(private_journal_dir)
    journal.prepare(operation, trusted)
    consume = OwnedOperation.consume

    def lose_ack(
        owned: OwnedOperation, *args: object, **kwargs: object
    ) -> Permit | None:
        # The real transaction has committed before its confirmation is lost.
        assert consume(owned, *args, **kwargs) is not None  # type: ignore[arg-type]
        raise JournalUnavailable

    if committed:
        monkeypatch.setattr(OwnedOperation, "consume", lose_ack)
    else:
        install_abort(private_journal_dir, "idempotency_key")
    calls: list[tuple[str, str]] = []
    stream = io.StringIO()
    executor = OperationExecutor(
        journal, 4, (JsonlEventSink(stream), EvidenceContext(uuid4(), "a" * 40))
    )

    def deliver() -> str:
        return asyncio.run(
            executor.execute(
                operation,
                trusted,
                TEXT,
                test_transport=httpx2.MockTransport(simulator_handler(calls)),
            )
        ).outcome

    assert deliver() == deliver() == "UNKNOWN"
    assert [method for method, _ in calls] == ["GET", "GET", "GET"]
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None
        state = owned.read()
        assert state.consumed == committed
        assert not state.handed_off and not state.checked and state.case_id is None
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["phase"] for row in rows] == ["STARTED", "FINISHED"] * 2
    assert all(
        row["outcome"] == "UNKNOWN" for row in rows if row["phase"] == "FINISHED"
    )
    assert "SYNTHETIC_JOURNAL_DETAIL" not in stream.getvalue() + capsys.readouterr().err


@pytest.mark.parametrize(
    "mode",
    [
        "found",
        "missing",
        "unavailable",
        "unauthorized",
        "timeout",
        "wrong-date",
        "check-commit-fails",
    ],
)
def test_reconciliation_uses_one_original_key(
    private_journal_dir: Path,
    mode: str,
) -> None:
    trusted, operation = context(), uuid4()
    journal = OperationJournal(private_journal_dir)
    journal.prepare(operation, trusted)
    if mode == "check-commit-fails":
        install_abort(private_journal_dir, "result_check_consumed")
    calls: list[tuple[str, str]] = []
    keys: list[str] = []
    read = simulator_handler(calls)

    async def send(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            calls.append(("POST", request.url.path))
            keys.append(request.headers["idempotency-key"])
            raise httpx2.ReadError("synthetic lost response")
        if request.url.path.endswith("/case-result"):
            calls.append(("CHECK", request.url.path))
            keys.append(request.headers["idempotency-key"])
            if request.headers["authorization"] != "Bearer " + trusted.bearer:
                pytest.fail("Unexpected trusted context", pytrace=False)
            if mode == "missing":
                return httpx2.Response(200, json={"outcome": "NOT_FOUND", "case": None})
            if mode == "unavailable":
                return httpx2.Response(
                    503, json={"error": {"code": "RESULT_CHECK_UNAVAILABLE"}}
                )
            if mode == "unauthorized":
                return httpx2.Response(401, json={"error": {"code": "UNAUTHENTICATED"}})
            if mode == "timeout":
                await asyncio.sleep(2)
            case = case_json()
            if mode == "wrong-date":
                case["requested_date"] = "2026-11-27"
            return httpx2.Response(200, json={"outcome": "FOUND", "case": case})
        return read(request)

    async def exercise() -> None:
        executor = OperationExecutor(journal, 0.5)
        first = await executor.execute(
            operation, trusted, TEXT, test_transport=httpx2.MockTransport(send)
        )
        replay = await executor.execute(
            operation, trusted, TEXT, test_transport=httpx2.MockTransport(send)
        )
        assert calls.count(("POST", "/cases")) == 1
        expected_checks = int(mode != "check-commit-fails")
        assert calls.count(("CHECK", f"/orders/{ORDER}/case-result")) == expected_checks
        if len(set(keys)) != 1:
            pytest.fail("The original key was not retained", pytrace=False)
        if mode == "found":
            assert first == replay
            assert first.case_id == CASE_ID and first.outcome == "EXISTING"
        else:
            assert (
                first.case_id is None and first.outcome == replay.outcome == "UNKNOWN"
            )
        with journal.ownership(operation, trusted) as owned:
            assert owned is not None
            assert owned.read().checked == bool(expected_checks)

    asyncio.run(exercise())


class CaptureSink:
    def __init__(self, write: Callable[[AttemptEvent], None]) -> None:
        self.events: list[AttemptEvent] = []
        self.write = write

    def emit(self, event: AttemptEvent) -> None:
        self.events.append(event)
        self.write(event)


def test_event_sink_keeps_correlated_attempt_history(private_journal_dir: Path) -> None:
    journal = OperationJournal(private_journal_dir)
    stream = io.StringIO()
    sink = CaptureSink(JsonlEventSink(stream).emit)
    ctx = EvidenceContext(uuid4(), "a" * 40)
    executor = OperationExecutor(journal, 3, (sink, ctx))
    trusted, operation = context(), uuid4()
    journal.prepare(operation, trusted)
    request_marker = "SYNTHETIC_USER_MARKER"
    response_marker = "SYNTHETIC_RESPONSE_MARKER"
    calls: list[tuple[str, str]] = []
    keys: list[str] = []
    read = simulator_handler(
        calls, on_post=lambda request: keys.append(request.headers["idempotency-key"])
    )

    def transport(request: httpx2.Request) -> httpx2.Response:
        response = read(request)
        body = response.json()
        body["synthetic_private"] = response_marker
        return httpx2.Response(response.status_code, json=body)

    async def exercise() -> None:
        first = await executor.execute(
            operation, trusted, TEXT, test_transport=httpx2.MockTransport(transport)
        )
        assert first.outcome == "CREATED"
        assert await executor.execute(operation, trusted, request_marker) == first
        next_id = uuid4()
        journal.prepare(next_id, trusted)
        stopped = await executor.execute(next_id, trusted, request_marker)
        assert stopped.outcome == "CLARIFY"

        pending = uuid4()
        journal.prepare(pending, trusted)
        reached = asyncio.Event()
        never = asyncio.Event()

        async def block(request: httpx2.Request) -> httpx2.Response:
            reached.set()
            await never.wait()
            return transport(request)

        child = asyncio.create_task(
            executor.execute(
                pending,
                trusted,
                TEXT,
                test_transport=httpx2.MockTransport(block),
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=3)
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        assert len(unfinished_attempts(sink.events)) == 1

    asyncio.run(exercise())
    evidence = stream.getvalue()
    assert len(keys) == 1
    for secret in (trusted.bearer, keys[0], request_marker, response_marker):
        if secret in evidence:
            pytest.fail("Sensitive material in evidence", pytrace=False)
    rows = [json.loads(line) for line in evidence.splitlines()]
    assert {row["series_id"] for row in rows} == {str(ctx.series_id)}
    assert {row["commit_sha"] for row in rows} == {ctx.commit_sha}
    assert len({row["attempt_id"] for row in rows}) == 4
    assert all(row["usage_source"] == "UNKNOWN" for row in rows)
    assert set(rows[-1]) == {
        "version",
        "commit_sha",
        "series_id",
        "attempt_id",
        "operation_id",
        "run_id",
        "phase",
        "outcome",
        "diagnostic",
        "utc",
        "elapsed_ms",
        "usage_source",
    }
