import asyncio
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from conftest import Project
from kt4_executor_support import private_journal_dir as private_journal_dir
from starlette.types import ASGIApp, Receive, Scope, Send
from support.kt3_fixture_loader import FIXTURE, FIXTURE_VERSION
from test_kt3_fixtures import (
    CLIENT_A,
    CLIENT_B,
    SAME_ORDER,
    create_run,
    initialize,
    snapshot,
)
from test_kt4_executor_core import DAY, TEXT
from test_kt4_tool_adapter_integration import build_simulator, case_ids, scheduled_date

from app.logistics_agent_executor import OperationExecutor
from app.logistics_agent_journal import OperationJournal
from app.logistics_agent_tools import CreateCaseResult, ToolAdapter, TrustedContext
from app.logistics_auth import LogisticsContext
from app.logistics_locks import order_lock


class PostBarrier:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.methods: list[tuple[str, str]] = []
        self.arrived = asyncio.Event()
        self.resume = asyncio.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.methods.append((scope["method"], scope["path"]))
            if scope["method"] == "POST":
                self.arrived.set()
                await asyncio.wait_for(self.resume.wait(), timeout=4)
        await self.app(scope, receive, send)


def test_kt4_executor_real_simulator_and_postgres(
    migrated_db: Project,
    private_journal_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = create_run(migrated_db), create_run(migrated_db)
    assert initialize(migrated_db, first)
    assert initialize(migrated_db, second)
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    for run in (first, second):
        assert snapshot(migrated_db, run)[0] == [(FIXTURE_VERSION, digest)]
    auth = tuple(
        LogisticsContext(run, client)
        for run in (first, second)
        for client in (CLIENT_A, CLIENT_B)
    )
    app, tokens = build_simulator(migrated_db, monkeypatch, *auth)
    contexts = tuple(
        TrustedContext(item.run_id, item.client_id, token, "http://executor.test", 2026)
        for item, token in zip(auth, tokens, strict=True)
    )
    stock = {
        (ctx.run_id, ctx.client_id): case_ids(migrated_db, ctx.run_id, ctx.client_id)
        for ctx in contexts
    }
    first_a, first_b, second_a, second_b = contexts
    journal = OperationJournal(private_journal_dir)
    executor = OperationExecutor(journal, timeout=8)

    async def check() -> None:
        async with app.router.lifespan_context(app):
            for context in contexts:
                operation = uuid4()
                journal.prepare(operation, context)
                wire = PostBarrier(app)
                output = await executor.execute(
                    operation,
                    context,
                    f"Покажите заказ {SAME_ORDER}",
                    test_transport=httpx2.ASGITransport(app=wire),
                )
                assert output.outcome == "INFORMATION" and output.order is not None
                assert output.order.scheduled_delivery_date == scheduled_date(
                    migrated_db, context.run_id, context.client_id
                )
                assert wire.methods == [("GET", f"/orders/{SAME_ORDER}")]

            foreign = uuid4()
            journal.prepare(foreign, first_a)
            wire = PostBarrier(app)
            denied = await executor.execute(
                foreign, second_b, TEXT, test_transport=httpx2.ASGITransport(app=wire)
            )
            assert denied.code == "OPERATION_JOURNAL_UNAVAILABLE"
            assert not wire.methods

            new = uuid4()
            journal.prepare(new, second_a)
            wire = PostBarrier(app)
            wire.resume.set()
            created = await executor.execute(
                new,
                second_a,
                f"Перенеси заказ {SAME_ORDER} на 2026-11-27",
                test_transport=httpx2.ASGITransport(app=wire),
            )
            assert created.outcome == "CREATED" and created.case_id is not None
            assert wire.methods[-1] == ("POST", "/cases")
            stock[(second, CLIENT_A)].add(created.case_id)

            duplicate = uuid4()
            journal.prepare(duplicate, first_a)
            wire = PostBarrier(app)
            output = await executor.execute(
                duplicate, first_a, TEXT, test_transport=httpx2.ASGITransport(app=wire)
            )
            assert output.outcome == "EXISTING"
            assert output.case_id in stock[(first, CLIENT_A)]
            assert all(method == "GET" for method, _ in wire.methods)

            async def run_at_barrier(ctx: TrustedContext, change: str) -> UUID | None:
                operation = uuid4()
                journal.prepare(operation, ctx)
                sent = PostBarrier(app)
                call = asyncio.create_task(
                    executor.execute(
                        operation,
                        ctx,
                        TEXT,
                        test_transport=httpx2.ASGITransport(app=sent),
                    )
                )
                try:
                    await asyncio.wait_for(sent.arrived.wait(), timeout=8)
                    assert sent.methods == [
                        ("GET", f"/orders/{SAME_ORDER}"),
                        ("GET", f"/orders/{SAME_ORDER}/delivery-options"),
                        ("GET", f"/orders/{SAME_ORDER}/cases"),
                        ("POST", "/cases"),
                    ]
                    assert not case_ids(migrated_db, ctx.run_id, ctx.client_id)
                    competing_id = None
                    if change == "existing":
                        async with ToolAdapter(
                            ctx, timeout=4, test_transport=httpx2.ASGITransport(app=app)
                        ) as tools:
                            prior = await tools.create_case(
                                {"order_id": SAME_ORDER, "requested_date": str(DAY)},
                                idempotency_key=uuid4().hex,
                            )
                            assert (
                                isinstance(prior, CreateCaseResult)
                                and prior.status == 201
                            )
                            competing_id = prior.case.case_id
                    else:
                        with migrated_db.db("migrate") as db:
                            args = (ctx.run_id, ctx.client_id, SAME_ORDER)
                            db.execute(
                                "SELECT pg_advisory_xact_lock(%s)", (order_lock(*args),)
                            )
                            if change == "blocked":
                                db.execute(
                                    "UPDATE qa.las_orders SET status='TRANSFER_BLOCKED' "
                                    "WHERE run_id=%s AND client_id=%s AND order_id=%s",
                                    args,
                                )
                            else:
                                db.execute(
                                    "DELETE FROM qa.las_delivery_dates WHERE run_id=%s "
                                    "AND client_id=%s AND order_id=%s AND delivery_date=%s",
                                    (*args, DAY),
                                )
                finally:
                    sent.resume.set()
                    output = await call
                if change == "existing":
                    assert (
                        output.outcome == "EXISTING" and output.case_id == competing_id
                    )
                else:
                    assert output.outcome == "REJECTED"
                    assert output.code == (
                        "ORDER_TRANSFER_BLOCKED"
                        if change == "blocked"
                        else "DELIVERY_DATE_NOT_ALLOWED"
                    )
                return competing_id

            await run_at_barrier(first_b, "blocked")
            await run_at_barrier(second_b, "dates")
            with migrated_db.db("migrate") as db:
                args = (first_b.run_id, first_b.client_id, SAME_ORDER)
                db.execute("SELECT pg_advisory_xact_lock(%s)", (order_lock(*args),))
                db.execute(
                    "UPDATE qa.las_orders SET status='ACTIVE' "
                    "WHERE run_id=%s AND client_id=%s AND order_id=%s",
                    args,
                )
            competing = await run_at_barrier(first_b, "existing")
            assert competing is not None
            stock[(first, CLIENT_B)].add(competing)
            assert stock[(second, CLIENT_A)] == case_ids(
                migrated_db, second_a.run_id, CLIENT_A
            )

    asyncio.run(check())
    for (run, client), expected in stock.items():
        assert case_ids(migrated_db, run, client) == expected
    output = capsys.readouterr()
    diagnostics = caplog.text + output.out + output.err
    for secret in (*tokens, *migrated_db.passwords.values()):
        if secret in diagnostics:
            pytest.fail("Test credentials reached diagnostics", pytrace=False)
