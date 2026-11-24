import asyncio
import hashlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from uuid import UUID, uuid4

import httpx2
import pytest
import uvicorn
from conftest import Project
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from test_kt3_fixtures import CLIENT_A, CLIENT_B, SAME_ORDER, create_run, initialize

from app.logistics_agent_tools import (
    CaseFound,
    CaseNotFound,
    CasesView,
    CreateCaseResult,
    DeliveryOptionsView,
    OrderView,
    ToolAdapter,
    ToolFailure,
    TrustedContext,
)
from app.logistics_auth import LogisticsContext
from app.main import create_app

DAY = "2026-11-26"
NOT_FOUND = ToolFailure("RESOURCE_NOT_FOUND", 404)


def build_simulator(
    project: Project, monkeypatch: pytest.MonkeyPatch, *contexts: LogisticsContext
) -> tuple[FastAPI, tuple[str, ...]]:
    project.api_env(monkeypatch)
    tokens = tuple(uuid4().hex for _ in contexts)
    app = create_app(
        {
            hashlib.sha256(token.encode()).hexdigest(): ctx
            for token, ctx in zip(tokens, contexts, strict=True)
        },
        case_write_timeout=4,
        case_result_timeout=2,
    )
    return app, tokens


def case_ids(project: Project, run: UUID, client: str) -> set[UUID]:
    with project.db() as observer:
        return {
            UUID(str(row[0]))
            for row in observer.execute(
                "SELECT case_id FROM qa.las_cases WHERE run_id=%s AND client_id=%s",
                (run, client),
            ).fetchall()
        }


def scheduled_date(project: Project, run: UUID, client: str) -> date:
    with project.db() as observer:
        row = observer.execute(
            "SELECT scheduled_delivery_date FROM qa.las_orders "
            "WHERE run_id=%s AND client_id=%s AND order_id=%s",
            (run, client, SAME_ORDER),
        ).fetchone()
    assert row is not None and isinstance(row[0], date)
    return row[0]


def test_kt4_simulator_asgi_and_postgres_isolation(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = create_run(migrated_db), create_run(migrated_db)
    assert initialize(migrated_db, first)
    assert initialize(migrated_db, second)
    contexts = (
        LogisticsContext(first, CLIENT_B),
        LogisticsContext(first, CLIENT_A),
        LogisticsContext(second, CLIENT_B),
    )
    app, tokens = build_simulator(migrated_db, monkeypatch, *contexts)
    origin = "http://synthetic-simulator.test"
    args = {"order_id": SAME_ORDER}
    payload = {**args, "requested_date": DAY}
    key = uuid4().hex
    assert not case_ids(migrated_db, first, CLIENT_B)
    assert not case_ids(migrated_db, second, CLIENT_B)
    existing_a = case_ids(migrated_db, first, CLIENT_A)
    assert len(existing_a) == 1
    before_date = scheduled_date(migrated_db, first, CLIENT_B)

    async def exercise() -> UUID:
        first_b, first_a, second_b = (
            TrustedContext(ctx.run_id, ctx.client_id, token, origin, 2026)
            for ctx, token in zip(contexts, tokens, strict=True)
        )
        async with (
            app.router.lifespan_context(app),
            ToolAdapter(
                first_b, timeout=5, test_transport=httpx2.ASGITransport(app=app)
            ) as own,
            ToolAdapter(
                first_a, timeout=5, test_transport=httpx2.ASGITransport(app=app)
            ) as other,
            ToolAdapter(
                second_b, timeout=5, test_transport=httpx2.ASGITransport(app=app)
            ) as peer,
            ToolAdapter(
                TrustedContext(first, CLIENT_B, uuid4().hex, origin),
                timeout=5,
                test_transport=httpx2.ASGITransport(app=app),
            ) as invalid,
        ):
            assert await invalid.read("get_order", args) == ToolFailure(
                "UNAUTHENTICATED", 401
            )
            order = await own.read("get_order", args)
            assert (
                isinstance(order, OrderView)
                and order.scheduled_delivery_date == before_date
            )
            dates_b = await own.read("get_delivery_options", args)
            dates_a = await other.read("get_delivery_options", args)
            assert isinstance(dates_b, DeliveryOptionsView) and dates_b.dates == [
                date.fromisoformat(DAY)
            ]
            assert isinstance(dates_a, DeliveryOptionsView) and len(dates_a.dates) == 2
            blocked = await other.read(
                "get_delivery_options", {"order_id": "order-blocked"}
            )
            empty = await other.read(
                "get_delivery_options", {"order_id": "order-no-dates"}
            )
            assert (
                isinstance(blocked, DeliveryOptionsView)
                and blocked.reason_code == "ORDER_TRANSFER_BLOCKED"
            )
            assert (
                isinstance(empty, DeliveryOptionsView)
                and empty.reason_code == "NO_AVAILABLE_DATES"
            )
            assert (
                await own.read("get_order", {"order_id": "order-blocked"}) == NOT_FOUND
            )
            assert await own.read("get_order", {"order_id": "absent"}) == NOT_FOUND
            assert (
                await own.read("get_case", {"case_id": str(next(iter(existing_a)))})
                == NOT_FOUND
            )
            assert await own.read(
                "get_order", {**args, "client_id": CLIENT_A}
            ) == ToolFailure("INVALID_ARGUMENTS")
            assert await own.read("load_fixture", args) == ToolFailure(
                "TOOL_NOT_ALLOWED"
            )
            assert await own.read("get_order_cases", args) == CasesView(cases=[])
            assert isinstance(
                await own.check_case_result(args, idempotency_key=key), CaseNotFound
            )
            assert await own.create_case(
                {**payload, "requested_date": "2026-11-28"}, idempotency_key=uuid4().hex
            ) == ToolFailure("DELIVERY_DATE_NOT_ALLOWED", 422)
            assert await other.create_case(
                {**payload, "order_id": "order-blocked"}, idempotency_key=uuid4().hex
            ) == ToolFailure("ORDER_TRANSFER_BLOCKED", 409)
            created = await own.create_case(payload, idempotency_key=key)
            assert isinstance(created, CreateCaseResult) and created.status == 201
            again = await own.create_case(payload, idempotency_key=key)
            assert again == CreateCaseResult(200, created.case)
            assert await own.read("get_order_cases", args) == CasesView(
                cases=[created.case]
            )
            assert (
                await own.read("get_case", {"case_id": str(created.case.case_id)})
                == created.case
            )
            found = await own.check_case_result(args, idempotency_key=key)
            assert isinstance(found, CaseFound) and found.case == created.case
            assert (
                await peer.read("get_case", {"case_id": str(created.case.case_id)})
                == NOT_FOUND
            )
            assert (
                await other.read("get_case", {"case_id": str(created.case.case_id)})
                == NOT_FOUND
            )
            assert await peer.read("get_order_cases", args) == CasesView(cases=[])
            assert isinstance(
                await peer.check_case_result(args, idempotency_key=key), CaseNotFound
            )
            return created.case.case_id

    created_id = asyncio.run(exercise())
    assert case_ids(migrated_db, first, CLIENT_B) == {created_id}
    assert not case_ids(migrated_db, second, CLIENT_B)
    assert case_ids(migrated_db, first, CLIENT_A) == existing_a
    assert scheduled_date(migrated_db, first, CLIENT_B) == before_date


@asynccontextmanager
async def serve_loopback(app: ASGIApp) -> AsyncIterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="critical", ws="none")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("Uvicorn exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)


def test_kt4_uvicorn_tcp_post_loss_and_database(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_run(migrated_db)
    assert initialize(migrated_db, run)
    app, (token,) = build_simulator(
        migrated_db, monkeypatch, LogisticsContext(run, CLIENT_B)
    )
    assert not case_ids(migrated_db, run, CLIENT_B)
    before_date = scheduled_date(migrated_db, run, CLIENT_B)
    key = uuid4().hex
    sent = 0
    # A broken environment proxy exposes a missing trust_env=False on the real client.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def interrupted_reply(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal sent
        if scope["type"] != "http" or scope["method"] != "POST":
            await app(scope, receive, send)
            return
        sent += 1

        async def truncate(message: Message) -> None:
            if message["type"] == "http.response.body":
                # The route produced its success body after the database commit.
                await send(
                    {"type": "http.response.body", "body": b"{", "more_body": True}
                )
                raise RuntimeError("Controlled test response interruption")
            await send(message)

        await app(scope, receive, truncate)

    async def exercise() -> UUID:
        args = {"order_id": SAME_ORDER}
        async with (
            serve_loopback(interrupted_reply) as origin,
            ToolAdapter(
                TrustedContext(run, CLIENT_B, token, origin), timeout=5
            ) as adapter,
        ):
            order = await adapter.read("get_order", args)
            assert isinstance(order, OrderView)
            result = await adapter.create_case(
                {**args, "requested_date": DAY}, idempotency_key=key
            )
            assert result == ToolFailure("TRANSPORT_ERROR", outcome_unknown=True)
            assert sent == 1
            found = await adapter.check_case_result(args, idempotency_key=key)
            assert isinstance(found, CaseFound)
            return found.case.case_id

    found_id = asyncio.run(exercise())
    assert sent == 1 and case_ids(migrated_db, run, CLIENT_B) == {found_id}
    assert scheduled_date(migrated_db, run, CLIENT_B) == before_date


def test_kt4_httpx2_is_installed_in_runtime_image(fresh: Project) -> None:
    result = fresh.command(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/app/.venv/bin/python",
        fresh.env["QA_APP_IMAGE"],
        "-c",
        "import httpx2; import app.logistics_agent_tools; print(httpx2.__version__)",
    )
    assert result.stdout.strip() == "2.16.2"
