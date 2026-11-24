import asyncio
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import httpx2
import pytest

from app.logistics_agent_tools import (
    READ_OPERATIONS,
    CaseFound,
    CaseNotFound,
    CasesView,
    CaseView,
    CreateCaseResult,
    DeliveryOptionsView,
    Operation,
    OrderView,
    ToolAdapter,
    ToolFailure,
    ToolView,
    TrustedContext,
)

ORDER = "order-001"
DAY = "2026-11-26"
CASE_ID = "4cae3620-8b62-4675-b425-cb88c9fe266d"
BEARER = "synthetic-bearer-check-kt4"
KEY = "synthetic-idempotency-check-kt4"
CREDENTIAL = "synthetic-provider-credential-check-kt4"
ORIGIN = "http://simulator.test"


def context(origin: str = ORIGIN) -> TrustedContext:
    return TrustedContext(uuid4(), "demo-client-a", BEARER, origin, 2026)


def case_body() -> dict[str, object]:
    return {
        "case_id": CASE_ID,
        "order_id": ORDER,
        "requested_date": DAY,
        "operation_type": "DELIVERY_RESCHEDULE",
        "status": "OPEN",
    }


def test_kt4_trusted_context_and_registry() -> None:
    ctx = context()
    assert BEARER not in repr(ctx)
    with pytest.raises(FrozenInstanceError):
        setattr(ctx, "client_id", "other")
    assert set(Operation) == {
        "get_order",
        "get_delivery_options",
        "get_order_cases",
        "get_case",
        "create_case",
        "check_case_result",
    }
    assert READ_OPERATIONS == {
        "get_order",
        "get_delivery_options",
        "get_order_cases",
        "get_case",
    }
    for origin in (
        "file:///tmp/a",
        "http://u:p@local",
        ORIGIN + "/extra",
        ORIGIN + "?x=1",
    ):
        with pytest.raises(ValueError, match="Invalid Simulator origin"):
            context(origin)


def test_kt4_declines_context_changes_and_privileged_tools() -> None:
    def blocked(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("Denied request reached the transport")

    async def run() -> None:
        async with ToolAdapter(
            context(), timeout=1, test_transport=httpx2.MockTransport(blocked)
        ) as adapter:
            for name in ("run_id", "client_id", "bearer", "url", "role", "method"):
                assert await adapter.read(
                    "get_order", {"order_id": ORDER, name: "x"}
                ) == (ToolFailure("INVALID_ARGUMENTS"))
            for operation in (
                "load_fixture",
                "set_fault",
                "create_case",
                "check_case_result",
                "https://example.invalid/",
            ):
                assert await adapter.read(
                    operation, {"order_id": ORDER}
                ) == ToolFailure("TOOL_NOT_ALLOWED")

    asyncio.run(run())


def test_kt4_strict_arguments_never_send_http() -> None:
    def blocked(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("Invalid arguments reached the transport")

    async def run() -> None:
        async with ToolAdapter(
            context(), timeout=1, test_transport=httpx2.MockTransport(blocked)
        ) as adapter:
            invalid_orders: tuple[object, ...] = (
                None,
                1,
                True,
                "",
                "..",
                "a/b",
                "x?y",
                "a%2fb",
                "a\x00",
                [],
            )
            for order_id in invalid_orders:
                assert await adapter.read("get_order", {"order_id": order_id}) == (
                    ToolFailure("INVALID_ARGUMENTS")
                )
            for case_id in (3, "bad", UUID(CASE_ID), CASE_ID.replace("-", "")):
                assert await adapter.read(
                    "get_case", {"case_id": case_id}
                ) == ToolFailure("INVALID_ARGUMENTS")
            for requested_date in (1, "20261126", "2026-02-30", "2026-11-26T00:00:00"):
                assert await adapter.create_case(
                    {"order_id": ORDER, "requested_date": requested_date},
                    idempotency_key=KEY,
                ) == ToolFailure("INVALID_ARGUMENTS")
            for bad_key in ("", "a\r\nb", "é" * 257):
                assert await adapter.check_case_result(
                    {"order_id": ORDER}, idempotency_key=bad_key
                ) == ToolFailure("INVALID_ARGUMENTS")
            assert await adapter.create_case(
                {"order_id": ORDER, "requested_date": DAY, "operation_type": "OTHER"},
                idempotency_key=KEY,
            ) == ToolFailure("INVALID_ARGUMENTS")

    asyncio.run(run())


def test_kt4_wire_contract_and_projection() -> None:
    orders = f"/orders/{ORDER}"
    case = case_body()
    case["credentials"] = CREDENTIAL
    cases = [
        (
            "GET",
            orders,
            200,
            {
                "order_id": ORDER,
                "status": "ACTIVE",
                "scheduled_delivery_date": "2026-11-25",
                "bearer": BEARER,
            },
        ),
        (
            "GET",
            orders + "/delivery-options",
            200,
            {
                "order_id": ORDER,
                "eligible": True,
                "reason_code": None,
                "dates": [DAY],
            },
        ),
        ("GET", orders + "/cases", 200, {"cases": []}),
        ("GET", f"/cases/{CASE_ID}", 200, case),
        ("POST", "/cases", 201, case),
        ("POST", "/cases", 200, case),
        (
            "GET",
            orders + "/case-result",
            200,
            {"outcome": "FOUND", "case": case, "key": KEY},
        ),
        ("GET", orders + "/case-result", 200, {"outcome": "NOT_FOUND", "case": None}),
    ]
    planned = iter(cases)
    calls: list[tuple[str, str]] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        method, path, status, body = next(planned)
        assert request.method == method and request.url.path == path
        assert request.url.host == "simulator.test" and not request.url.query
        assert request.headers["authorization"] == f"Bearer {BEARER}"
        keyed = method == "POST" or path.endswith("/case-result")
        assert request.headers.get("idempotency-key") == (KEY if keyed else None)
        if method == "POST":
            assert json.loads(request.content) == {
                "order_id": ORDER,
                "requested_date": DAY,
                "operation_type": "DELIVERY_RESCHEDULE",
            }
        else:
            assert request.content == b""
        calls.append((method, path))
        return httpx2.Response(status, json=body)

    async def run() -> None:
        args: Mapping[str, object] = {"order_id": ORDER}
        async with ToolAdapter(
            context(), timeout=1, test_transport=httpx2.MockTransport(send)
        ) as adapter:
            order = await adapter.read("get_order", args)
            options = await adapter.read("get_delivery_options", args)
            listed = await adapter.read("get_order_cases", args)
            read = await adapter.read("get_case", {"case_id": CASE_ID})
            created = await adapter.create_case(
                {**args, "requested_date": DAY}, idempotency_key=KEY
            )
            existing = await adapter.create_case(
                {**args, "requested_date": DAY}, idempotency_key=KEY
            )
            found = await adapter.check_case_result(args, idempotency_key=KEY)
            missing = await adapter.check_case_result(args, idempotency_key=KEY)
        assert isinstance(order, OrderView) and order.order_id == ORDER
        assert (
            isinstance(options, DeliveryOptionsView)
            and options.dates[0].isoformat() == DAY
        )
        assert isinstance(listed, CasesView) and not listed.cases
        assert isinstance(read, CaseView) and str(read.case_id) == CASE_ID
        assert isinstance(created, CreateCaseResult) and created.status == 201
        assert isinstance(existing, CreateCaseResult) and existing.status == 200
        assert isinstance(found, CaseFound) and found.case == created.case
        assert isinstance(missing, CaseNotFound)
        projection = " ".join(
            value.model_dump_json()
            for value in (
                order,
                options,
                listed,
                read,
                created.case,
                existing.case,
                found,
                missing,
            )
        )
        assert not any(secret in projection for secret in (BEARER, KEY, CREDENTIAL))

    asyncio.run(run())
    assert len(calls) == len(cases)


@pytest.mark.parametrize(
    ("status", "code", "route"),
    [
        (401, "UNAUTHENTICATED", "read"),
        (404, "RESOURCE_NOT_FOUND", "read"),
        (409, "ORDER_TRANSFER_BLOCKED", "create"),
        (409, "IDEMPOTENCY_KEY_CONFLICT", "create"),
        (422, "DELIVERY_DATE_NOT_ALLOWED", "create"),
        (422, "VALIDATION_ERROR", "create"),
        (422, "VALIDATION_ERROR", "result"),
        (503, "RESULT_CHECK_UNAVAILABLE", "result"),
    ],
)
def test_kt4_classifies_public_errors(status: int, code: str, route: str) -> None:
    def send(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, json={"error": {"code": code, "text": CREDENTIAL}}
        )

    async def run() -> None:
        args: Mapping[str, object] = {"order_id": ORDER}
        async with ToolAdapter(
            context(), timeout=1, test_transport=httpx2.MockTransport(send)
        ) as adapter:
            outcome: (
                ToolView | ToolFailure | CreateCaseResult | CaseFound | CaseNotFound
            )
            if route == "read":
                outcome = await adapter.read("get_order", args)
            elif route == "create":
                outcome = await adapter.create_case(
                    {**args, "requested_date": DAY}, idempotency_key=KEY
                )
            else:
                outcome = await adapter.check_case_result(args, idempotency_key=KEY)
        assert outcome == ToolFailure(code, status)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["redirect", "malformed", "mismatch", "error", "lost", "timeout"]
)
def test_kt4_post_uncertainty_and_no_retry(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://unused-proxy.invalid:81")
    monkeypatch.setenv("HTTPS_PROXY", "http://unused-proxy.invalid:81")
    calls: list[str] = []

    async def send(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        if fault == "redirect":
            return httpx2.Response(
                307, headers={"location": "http://other.invalid/cases"}
            )
        if fault == "malformed":
            return httpx2.Response(201, content=b"{")
        if fault == "mismatch":
            return httpx2.Response(201, json={**case_body(), "order_id": "another"})
        if fault == "error":
            return httpx2.Response(500, json={"error": CREDENTIAL})
        if fault == "timeout":
            await asyncio.sleep(1)
        raise httpx2.ReadError(CREDENTIAL, request=request)

    async def run() -> None:
        async with ToolAdapter(
            context(), timeout=0.05, test_transport=httpx2.MockTransport(send)
        ) as adapter:
            result = await adapter.create_case(
                {"order_id": ORDER, "requested_date": DAY}, idempotency_key=KEY
            )
        assert isinstance(result, ToolFailure) and result.outcome_unknown
        expected = (
            "TRANSPORT_ERROR" if fault in {"lost", "timeout"} else "UNEXPECTED_RESPONSE"
        )
        assert result.code == expected and CREDENTIAL not in repr(result)

    asyncio.run(run())
    assert calls == ["/cases"]


def test_kt4_rejects_secrets_reflected_in_known_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="httpx2")
    calls: list[str] = []

    def send(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        if request.url.path.startswith("/cases/"):
            return httpx2.Response(
                200, json={**case_body(), "order_id": f"echo-{BEARER}"}
            )
        return httpx2.Response(
            200,
            json={
                "outcome": "FOUND",
                "case": {**case_body(), "order_id": f"echo-{KEY}"},
            },
        )

    async def run() -> None:
        async with ToolAdapter(
            context(), timeout=1, test_transport=httpx2.MockTransport(send)
        ) as adapter:
            assert await adapter.read(
                "get_order", {"order_id": f"echo-{BEARER}"}
            ) == ToolFailure("INVALID_ARGUMENTS")
            assert await adapter.create_case(
                {"order_id": f"echo-{KEY}", "requested_date": DAY}, idempotency_key=KEY
            ) == ToolFailure("INVALID_ARGUMENTS")
            assert await adapter.check_case_result(
                {"order_id": f"echo-{KEY}"}, idempotency_key=KEY
            ) == ToolFailure("INVALID_ARGUMENTS")
            assert await adapter.read("get_case", {"case_id": CASE_ID}) == ToolFailure(
                "UNEXPECTED_RESPONSE", 200
            )
            assert await adapter.check_case_result(
                {"order_id": ORDER}, idempotency_key=KEY
            ) == ToolFailure("UNEXPECTED_RESPONSE", 200)

    asyncio.run(run())
    assert calls == [f"/cases/{CASE_ID}", f"/orders/{ORDER}/case-result"]
    assert BEARER not in caplog.text and KEY not in caplog.text
