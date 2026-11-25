import asyncio
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from kt4_executor_support import private_journal_dir as private_journal_dir

from app.logistics_agent_executor import ExecutionResult, OperationExecutor
from app.logistics_agent_intent import RequestIntent, resolve_intent
from app.logistics_agent_journal import OperationJournal
from app.logistics_agent_tools import TrustedContext

ORDER = "order-001"
DAY = date(2026, 11, 26)
TEXT = "Хочу перенести заказ order-001 на 26 ноября"
CASE_ID = UUID("b7da32ea-2e35-4c5a-84d6-db0102684df2")
ORIGIN = "http://simulator.test"


def context() -> TrustedContext:
    return TrustedContext(uuid4(), "synthetic-client", uuid4().hex, ORIGIN, 2026)


@pytest.mark.parametrize(
    ("text", "year", "expected"),
    [
        (TEXT, 2026, RequestIntent(ORDER, DAY, True)),
        (
            "Хочу перенести заказ 12345 на 26 ноября",
            2026,
            RequestIntent("12345", DAY, True),
        ),
        (
            "Перенесите заказ 12347 ещё раз на 26 ноября",
            2026,
            RequestIntent("12347", DAY, True),
        ),
        (
            "Прошу перенести доставку заказа order-001 на 2026-11-26.",
            None,
            RequestIntent(ORDER, DAY),
        ),
        (
            "Перенесите заказ order-001 на 26 ноября 2026 года",
            2030,
            RequestIntent(ORDER, DAY),
        ),
        ("Перенесите заказ order-001 на 26.11", 2026, RequestIntent(ORDER, DAY, True)),
        ("Покажите заказ order-001", 2026, RequestIntent(ORDER, None)),
        ("Можно ли перенести заказ 12345?", 2026, RequestIntent("12345", None)),
        (TEXT, None, None),
        ("Перенесите заказ 123 на 26 ноября", 2026, None),
        ("Перенесите заказ order-0 на 26 ноября", 2026, None),
        ("Перенесите заказ order- на 26 ноября", 2026, None),
        ("Перенесите заказ 12345 или 12346 на 26 ноября", 2026, None),
        ("Перенесите заказ 12345 на 26 или 27 ноября", 2026, None),
        ("Перенесите заказ 12345 на 26 ноября или 27 ноября", 2026, None),
        ("Перенесите заказ 12345 на пятницу", 2026, None),
        ("Перенесите заказ 12345 на 2026-02-30", 2026, None),
        ("Перенесите мою доставку на пятницу", 2026, None),
        ("Можно ли перенести заказ 12345 на 26 ноября?", 2026, None),
        ("Не переносите заказ order-001 на 26 ноября", 2026, None),
        ("Если будет удобно, перенесите заказ order-001 на 26 ноября", 2026, None),
        ('Он сказал: "Перенесите заказ order-001 на 26 ноября"', 2026, None),
    ],
)
def test_kt4_intent(
    text: str, year: int | None, expected: RequestIntent | None
) -> None:
    assert resolve_intent(text, year) == expected


def case_json() -> dict[str, str]:
    return {
        "case_id": str(CASE_ID),
        "order_id": ORDER,
        "operation_type": "DELIVERY_RESCHEDULE",
        "requested_date": DAY.isoformat(),
        "status": "OPEN",
    }


def simulator_handler(
    calls: list[tuple[str, str]],
    *,
    blocked: bool = False,
    dates: tuple[str, ...] = ("2026-11-26",),
    duplicate: bool = False,
    post_status: int = 201,
    on_post: Callable[[httpx2.Request], None] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    def handle(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        match request.method, request.url.path:
            case "GET", "/orders/order-001":
                return httpx2.Response(
                    200,
                    json={
                        "order_id": ORDER,
                        "status": "TRANSFER_BLOCKED" if blocked else "ACTIVE",
                        "scheduled_delivery_date": "2026-11-20",
                    },
                )
            case "GET", "/orders/order-001/delivery-options":
                return httpx2.Response(
                    200,
                    json={
                        "order_id": ORDER,
                        "eligible": bool(dates),
                        "reason_code": None
                        if dates
                        else (
                            "ORDER_TRANSFER_BLOCKED"
                            if blocked
                            else "NO_AVAILABLE_DATES"
                        ),
                        "dates": list(dates),
                    },
                )
            case "GET", "/orders/order-001/cases":
                return httpx2.Response(
                    200, json={"cases": [case_json()] if duplicate else []}
                )
            case "POST", "/cases":
                if on_post:
                    on_post(request)
                assert json.loads(request.content) == {
                    "order_id": ORDER,
                    "requested_date": str(DAY),
                    "operation_type": "DELIVERY_RESCHEDULE",
                }
                if post_status == 409:
                    return httpx2.Response(
                        409, json={"error": {"code": "ORDER_TRANSFER_BLOCKED"}}
                    )
                return httpx2.Response(post_status, json=case_json())
            case _:
                pytest.fail("Unexpected operation or route")

    return handle


@pytest.mark.parametrize(
    ("text", "blocked", "dates", "duplicate", "status", "outcome", "code", "methods"),
    [
        (
            TEXT,
            False,
            (str(DAY),),
            False,
            201,
            "CREATED",
            "CASE_CREATED",
            ["GET", "GET", "GET", "POST"],
        ),
        (
            TEXT,
            False,
            (str(DAY),),
            False,
            200,
            "EXISTING",
            "OPEN_CASE_EXISTS",
            ["GET", "GET", "GET", "POST"],
        ),
        (
            TEXT,
            False,
            (str(DAY),),
            False,
            409,
            "REJECTED",
            "ORDER_TRANSFER_BLOCKED",
            ["GET", "GET", "GET", "POST"],
        ),
        (
            TEXT,
            False,
            (str(DAY),),
            True,
            201,
            "EXISTING",
            "OPEN_CASE_EXISTS",
            ["GET", "GET", "GET"],
        ),
        (
            TEXT,
            True,
            (),
            False,
            201,
            "REJECTED",
            "ORDER_TRANSFER_BLOCKED",
            ["GET", "GET"],
        ),
        (TEXT, False, (), False, 201, "REJECTED", "NO_AVAILABLE_DATES", ["GET", "GET"]),
        (
            "Перенесите заказ order-001 на 2026-11-27",
            False,
            (str(DAY),),
            False,
            201,
            "REJECTED",
            "DELIVERY_DATE_NOT_ALLOWED",
            ["GET", "GET"],
        ),
        (
            TEXT,
            False,
            (str(DAY), str(DAY)),
            False,
            201,
            "CLARIFY",
            "DATE_REQUIRES_CLARIFICATION",
            ["GET", "GET"],
        ),
        (
            "Покажите заказ order-001",
            False,
            (str(DAY),),
            False,
            201,
            "INFORMATION",
            "ORDER_READ",
            ["GET"],
        ),
        (
            "Перенесите заказ order-0 на 26 ноября",
            False,
            (str(DAY),),
            False,
            201,
            "CLARIFY",
            "ORDER_OR_DATE_REQUIRED",
            [],
        ),
    ],
)
def test_kt4_executor_gates(
    private_journal_dir: Path,
    text: str,
    blocked: bool,
    dates: tuple[str, ...],
    duplicate: bool,
    status: int,
    outcome: str,
    code: str,
    methods: list[str],
) -> None:
    journal = OperationJournal(private_journal_dir)
    trusted, operation = context(), uuid4()
    journal.prepare(operation, trusted)
    calls: list[tuple[str, str]] = []
    transport = httpx2.MockTransport(
        simulator_handler(
            calls, blocked=blocked, dates=dates, duplicate=duplicate, post_status=status
        )
    )
    result = asyncio.run(
        OperationExecutor(journal, 2).execute(
            operation, trusted, text, test_transport=transport
        )
    )
    assert (result.outcome, result.code) == (outcome, code)
    assert [method for method, _ in calls] == methods
    assert [path for _, path in calls] == [
        "/orders/order-001",
        "/orders/order-001/delivery-options",
        "/orders/order-001/cases",
        "/cases",
    ][: len(methods)]
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None
        state = owned.read()
        assert state.consumed == ("POST" in methods)
        assert state.handed_off == state.consumed
        if "POST" in methods and status in {200, 201}:
            assert state.case_id == result.case_id == CASE_ID
            assert state.response_status == status
        else:
            assert state.case_id is None


def test_kt4_unconfirmed_permit_never_reaches_post(
    private_journal_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.logistics_agent_journal import OwnedOperation

    journal = OperationJournal(private_journal_dir)
    trusted, operation = context(), uuid4()
    journal.prepare(operation, trusted)

    monkeypatch.setattr(OwnedOperation, "consume", lambda *_: None)
    calls: list[tuple[str, str]] = []
    transport = httpx2.MockTransport(simulator_handler(calls))
    result = asyncio.run(
        OperationExecutor(journal, 2).execute(
            operation, trusted, TEXT, test_transport=transport
        )
    )
    assert result == ExecutionResult("UNKNOWN", "POST_PERMIT_NOT_CONFIRMED")
    assert [method for method, _ in calls] == ["GET", "GET", "GET"]
    with journal.ownership(operation, trusted) as owned:
        assert owned is not None and not owned.read().consumed


def test_kt4_post_observes_durable_consumption(
    private_journal_dir: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sqlite3
    from contextlib import closing

    journal = OperationJournal(private_journal_dir)
    trusted, operation = context(), uuid4()
    journal.prepare(operation, trusted)
    calls: list[tuple[str, str]] = []

    used_keys: list[str] = []

    def inspect(request: httpx2.Request) -> None:
        used_keys.append(request.headers["idempotency-key"])
        with closing(sqlite3.connect(private_journal_dir / "operations.sqlite3")) as db:
            observed = db.execute(
                "SELECT idempotency_key, transport_observed, case_id FROM operations "
                "WHERE operation_id=?",
                (str(operation),),
            ).fetchone()
        if observed != (request.headers["idempotency-key"], "UNOBSERVED", None):
            pytest.fail(
                "POST did not observe a separately committed permit", pytrace=False
            )

    transport = httpx2.MockTransport(simulator_handler(calls, on_post=inspect))
    result = asyncio.run(
        OperationExecutor(journal, 2).execute(
            operation, trusted, TEXT, test_transport=transport
        )
    )
    assert result == ExecutionResult("CREATED", "CASE_CREATED", CASE_ID)
    assert calls.count(("POST", "/cases")) == 1
    captured = capsys.readouterr()
    diagnostic = repr(result) + caplog.text + captured.out + captured.err
    if any(secret in diagnostic for secret in (trusted.bearer, *used_keys)):
        pytest.fail("Private material appeared in diagnostics", pytrace=False)
