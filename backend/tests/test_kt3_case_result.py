import asyncio
import hashlib
import json
import logging
import secrets
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2 as httpx
import pytest
from conftest import Project
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from test_kt3_case_concurrency import case_rows, post_case
from test_kt3_fixtures import CLIENT_A, CLIENT_B, SAME_ORDER, create_run, initialize
from test_kt3_order_reads import MISSING, add_order, authorization, snapshot

from app.case_store import READ_RESULT, CaseStore
from app.database import Database
from app.logistics_auth import LogisticsContext
from app.logistics_faults import FaultHook, FaultPhase, ResultCheckUnavailable, no_fault
from app.main import create_app

RESULT = f"/orders/{SAME_ORDER}/case-result"
ABSENT = {"outcome": "NOT_FOUND", "case": None}
UNAVAILABLE = {"error": {"code": "RESULT_CHECK_UNAVAILABLE"}}
FaultAction = Callable[[], Awaitable[None]]


@dataclass
class Profiles:
    # Phase names are disjoint across write and result operations.
    actions: dict[tuple[LogisticsContext, FaultPhase, int], FaultAction]
    calls: dict[tuple[LogisticsContext, FaultPhase], int] = field(default_factory=dict)

    async def __call__(self, context: LogisticsContext, phase: FaultPhase) -> None:
        target = (context, phase)
        count = self.calls.get(target, 0) + 1
        self.calls[target] = count
        action = self.actions.get((*target, count))
        if action is not None:
            await action()


class AbortCaseWrite(RuntimeError):
    """Abort the transaction before commit in the isolated test."""


@dataclass
class DeliveryFault:
    armed: bool = False
    statuses: list[int] = field(default_factory=list)
    posts: int = 0
    checks: int = 0

    async def drop_after_commit(self) -> None:
        self.armed = True

    async def abort_before_commit(self) -> None:
        self.armed = True
        raise AbortCaseWrite()


class FaultyDelivery(httpx.AsyncBaseTransport):
    def __init__(self, app: httpx.ASGITransport, fault: DeliveryFault):
        self.app = app
        self.fault = fault

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.fault.posts += 1
        if request.method == "GET":
            self.fault.checks += 1
        response = await self.app.handle_async_request(request)
        if self.fault.armed and request.method == "POST":
            self.fault.armed = False
            self.fault.statuses.append(response.status_code)
            await response.aclose()
            raise httpx.ReadError(
                "synthetic response delivery failure", request=request
            )
        return response


async def unavailable() -> None:
    raise ResultCheckUnavailable()


@asynccontextmanager
async def result_app(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    *contexts: LogisticsContext,
    fault: FaultHook = no_fault,
    result_timeout: float = 2,
    delivery: DeliveryFault | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, tuple[str, ...], CaseStore]]:
    project.api_env(monkeypatch)
    tokens = tuple(secrets.token_urlsafe(32) for _ in contexts)
    app = create_app(
        {
            hashlib.sha256(token.encode()).hexdigest(): context
            for token, context in zip(tokens, contexts, strict=True)
        },
        case_write_timeout=4,
        case_result_timeout=result_timeout,
        case_fault=fault,
    )
    asgi = httpx.ASGITransport(app=app, raise_app_exceptions=delivery is None)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=asgi if delivery is None else FaultyDelivery(asgi, delivery),
            base_url="http://synthetic-test",
        ) as client,
    ):
        yield client, tokens, app.state.case_store


async def result(
    client: httpx.AsyncClient, token: str, key: str, path: str = RESULT
) -> httpx.Response:
    return await client.get(
        path, headers={**authorization(token), "Idempotency-Key": key}
    )


def seeded(project: Project, owner: str = CLIENT_B) -> LogisticsContext:
    run = create_run(project)
    assert initialize(project, run)
    return LogisticsContext(run, owner)


def test_case_result_contract(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        owner, neighbor = seeded(migrated_db), seeded(migrated_db)
        other_client = LogisticsContext(owner.run_id, CLIENT_A)
        add_order(migrated_db, owner.run_id, CLIENT_B, "owner-only")
        async with result_app(
            migrated_db, monkeypatch, owner, other_client, neighbor
        ) as (client, (a, other, b), _):
            first, second = await asyncio.gather(
                post_case(client, "original", token=a),
                post_case(client, "alias", token=a),
            )
            assert sorted((first.status_code, second.status_code)) == [200, 201]
            assert second.json() == first.json()
            before = snapshot(migrated_db)
            for key in ("original", "alias"):
                found = await result(client, a, key)
                assert (found.status_code, found.json()) == (
                    200,
                    {"outcome": "FOUND", "case": first.json()},
                )
            for token, key in ((a, "no-key"), (b, "original"), (other, "original")):
                missing = await result(client, token, key)
                assert (missing.status_code, missing.json()) == (200, ABSENT)
            mismatched = await result(
                client, a, "original", "/orders/owner-only/case-result"
            )
            assert (mismatched.status_code, mismatched.json()) == (200, ABSENT)
            spoofed = await client.get(
                RESULT,
                params={"run_id": str(owner.run_id), "client_id": CLIENT_B},
                headers={
                    **authorization(b),
                    "Idempotency-Key": "original",
                    "X-Run-Id": str(owner.run_id),
                    "X-Client-Id": CLIENT_B,
                },
            )
            assert (spoofed.status_code, spoofed.json()) == (200, ABSENT)
            for token, order in (
                (a, "absent"),
                (other, "owner-only"),
                (b, "owner-only"),
            ):
                missing = await result(
                    client, token, "original", f"/orders/{order}/case-result"
                )
                assert (missing.status_code, missing.content) == (404, MISSING)
            for raw_key in (b"k" * 512, ("é" * 256).encode(), b"inside\tkey"):
                good = await client.get(
                    RESULT,
                    headers=[
                        (b"authorization", f"Bearer {a}".encode()),
                        (b"idempotency-key", raw_key),
                    ],
                )
                assert (good.status_code, good.json()) == (200, ABSENT)
            for keys in (
                [],
                [b""],
                [b" "],
                [b"\xff"],
                [b"x\x00"],
                [b"k" * 513],
                [("é" * 256).encode() + b"x"],
                [b"a", b"b"],
            ):
                bad = await client.get(
                    RESULT,
                    headers=[
                        (b"authorization", f"Bearer {a}".encode()),
                        *((b"idempotency-key", key) for key in keys),
                    ],
                )
                assert (bad.status_code, bad.json()) == (
                    422,
                    {"error": {"code": "VALIDATION_ERROR"}},
                )
            unauthorized = await result(client, "invalid", "x" * 513)
            assert (unauthorized.status_code, unauthorized.json()) == (
                401,
                {"error": {"code": "UNAUTHENTICATED"}},
            )
            no_bearer = await client.get(RESULT)
            assert (no_bearer.status_code, no_bearer.json()) == (
                401,
                unauthorized.json(),
            )
            assert snapshot(migrated_db) == before

    asyncio.run(exercise())


def test_case_result_before_commit(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        owner = seeded(migrated_db)
        reached, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            reached.set()
            await asyncio.wait_for(release.wait(), 3)

        profiles = Profiles({(owner, "before_commit", 1): hold})
        async with result_app(migrated_db, monkeypatch, owner, fault=profiles) as (
            client,
            (token,),
            store,
        ):
            before = snapshot(migrated_db)
            post = asyncio.create_task(post_case(client, "pending", token=token))
            try:
                await asyncio.wait_for(reached.wait(), 2)
                read = await asyncio.wait_for(result(client, token, "pending"), 1.5)
                assert (read.status_code, read.json()) == (200, ABSENT)
                pool = cast(AsyncAdaptedQueuePool, store.database.engine.pool)
                assert not post.done() and pool.checkedout() == 1
                assert snapshot(migrated_db) == before
            finally:
                release.set()
                await asyncio.gather(post, return_exceptions=True)
            assert post.result().status_code == 201
            cases, keys = case_rows(migrated_db, owner.run_id)
            assert len(cases) == 1 and keys == [("pending", cases[0][0])]
            assert profiles.calls == {
                (owner, "before_commit"): 1,
                (owner, "after_commit"): 1,
                (owner, "before_result_read"): 1,
            }

    asyncio.run(exercise())


def test_case_result_fault_profiles(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        before, after, repeated, peer = (seeded(migrated_db) for _ in range(4))
        same_run_client = LogisticsContext(after.run_id, CLIENT_A)
        delivery = DeliveryFault()
        entered, release = asyncio.Event(), asyncio.Event()

        async def coordinated_unavailability() -> None:
            entered.set()
            await asyncio.wait_for(release.wait(), 1.5)
            await unavailable()

        await_faults: dict[tuple[LogisticsContext, FaultPhase, int], FaultAction] = {
            (before, "before_commit", 1): delivery.abort_before_commit,
            (repeated, "before_commit", 1): delivery.abort_before_commit,
            (after, "after_commit", 1): delivery.drop_after_commit,
            (after, "before_result_read", 2): coordinated_unavailability,
        }
        profiles = Profiles(await_faults)
        async with result_app(
            migrated_db,
            monkeypatch,
            before,
            after,
            repeated,
            same_run_client,
            peer,
            fault=profiles,
            delivery=delivery,
        ) as (client, tokens, _):
            for context, token, outcome in zip(
                (before, after, repeated),
                tokens[:3],
                ("NOT_FOUND", "FOUND", "NOT_FOUND"),
                strict=True,
            ):
                posts, checks = delivery.posts, delivery.checks
                with pytest.raises(httpx.ReadError):
                    await post_case(client, "unknown", token=token)
                prior = snapshot(migrated_db)
                found = await result(client, token, "unknown")
                assert found.status_code == 200 and found.json()["outcome"] == outcome
                cases, keys = case_rows(migrated_db, context.run_id)
                if outcome == "FOUND":
                    assert len(cases) == 1 and keys == [("unknown", cases[0][0])]
                    assert found.json()["case"]["case_id"] == str(cases[0][0])
                else:
                    assert found.json() == ABSENT and not cases and not keys
                assert snapshot(migrated_db) == prior
                assert profiles.calls[(context, "before_commit")] == 1
                assert profiles.calls[(context, "before_result_read")] == 1
                assert (delivery.posts - posts, delivery.checks - checks) == (1, 1)
            assert delivery.statuses == [500, 201, 500]
            peer_case = await post_case(client, "unknown", token=tokens[4])
            assert peer_case.status_code == 201
            cursor = snapshot(migrated_db)
            faulted = asyncio.create_task(result(client, tokens[1], "unknown"))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                peer_reply = await result(client, tokens[4], "unknown")
                assert (peer_reply.status_code, peer_reply.json()) == (
                    200,
                    {"outcome": "FOUND", "case": peer_case.json()},
                )
                attempted_control = await client.request(
                    "GET",
                    RESULT + "?fault=before_result_read",
                    headers={
                        **authorization(tokens[3]),
                        "Idempotency-Key": "unknown",
                        "X-Test-Fault": "before_result_read",
                    },
                    json={"fault": "before_result_read"},
                )
                assert (attempted_control.status_code, attempted_control.json()) == (
                    200,
                    ABSENT,
                )
                assert not faulted.done()
            finally:
                release.set()
                await asyncio.gather(faulted, return_exceptions=True)
            failed = faulted.result()
            assert (failed.status_code, failed.json()) == (503, UNAVAILABLE)
            recovered = await result(client, tokens[1], "unknown")
            assert (
                recovered.status_code == 200 and recovered.json()["outcome"] == "FOUND"
            )
            assert snapshot(migrated_db) == cursor

    asyncio.run(exercise())


def test_case_result_database_and_pool_recovery(
    migrated_db: Project,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        owner = seeded(migrated_db)
        caplog.set_level(logging.INFO, logger="app.logistics_cases")
        async with result_app(migrated_db, monkeypatch, owner, result_timeout=0.4) as (
            client,
            (token,),
            store,
        ):
            before = snapshot(migrated_db)
            engine = store.database.engine
            async with engine.connect(), engine.connect():
                started = time.monotonic()
                reply = await result(client, token, "unconfirmed-synthetic")
                assert (reply.status_code, reply.json()) == (503, UNAVAILABLE)
                assert (
                    0.35
                    <= time.monotonic() - started
                    < store.database.settings.pool_timeout
                )
            assert cast(AsyncAdaptedQueuePool, engine.pool).checkedout() == 0
            recovered = await result(client, token, "unconfirmed-synthetic")
            assert (recovered.status_code, recovered.json()) == (200, ABSENT)
            assert snapshot(migrated_db) == before
            migrated_db.compose("stop", "db")
            reply = await result(client, token, "unconfirmed-synthetic")
            assert (reply.status_code, reply.json()) == (503, UNAVAILABLE)
            await store.database.drain_cleanup()
            migrated_db.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
            recovered = await result(client, token, "unconfirmed-synthetic")
            assert (recovered.status_code, recovered.json()) == (200, ABSENT)
            assert snapshot(migrated_db) == before
            assert "code=RESULT_CHECK_UNAVAILABLE" in caplog.text
            assert (
                token not in caplog.text and "unconfirmed-synthetic" not in caplog.text
            )

    asyncio.run(exercise())


def test_case_result_phase_measurements(
    migrated_db: Project,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise() -> None:
        owner = seeded(migrated_db)
        with migrated_db.db() as observer:
            assert observer.info.server_version == 180007
        phases: dict[str, list[float]] = {}
        label = "cold"

        def record(phase: str, started: float) -> None:
            phases.setdefault(f"{label}/{phase}", []).append(
                (time.perf_counter() - started) * 1000
            )

        async with result_app(migrated_db, monkeypatch, owner, result_timeout=0.6) as (
            client,
            (token,),
            store,
        ):
            assert (
                await post_case(client, "measured-result", token=token)
            ).status_code == 201
            before = snapshot(migrated_db)
            connect, execute, release = (
                AsyncEngine.connect,
                AsyncConnection.execute,
                Database._release,
            )

            async def measure_connect(engine: AsyncEngine) -> AsyncConnection:
                started = time.perf_counter()
                try:
                    return await connect(engine)
                finally:
                    record("checkout_ms", started)

            async def measure_execute(
                conn: AsyncConnection, statement: Any, params: Any = None, **kwargs: Any
            ) -> Any:
                started = time.perf_counter()
                try:
                    return await execute(conn, statement, params, **kwargs)
                finally:
                    if statement is READ_RESULT:
                        record("sql_ms", started)

            async def measure_release(
                database: Database, conn: AsyncConnection, *, invalidate: bool = False
            ) -> None:
                started = time.perf_counter()
                try:
                    await release(database, conn, invalidate=invalidate)
                finally:
                    record("cleanup_ms", started)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncEngine, "connect", measure_connect)
                patch.setattr(AsyncConnection, "execute", measure_execute)
                patch.setattr(Database, "_release", measure_release)
                for label in ("cold", "warm", "missing"):
                    for _ in range(5):
                        if label == "cold":
                            await store.database.engine.dispose()
                        started = time.perf_counter()
                        response = await result(
                            client,
                            token,
                            "absent" if label == "missing" else "measured-result",
                        )
                        record("http_ms", started)
                        assert response.status_code == 200
                        assert response.json()["outcome"] == (
                            "NOT_FOUND" if label == "missing" else "FOUND"
                        )
                label = "sql_deadline"
                for _ in range(3):
                    with migrated_db.db("migrate") as holder:
                        holder.execute(
                            "LOCK TABLE qa.las_orders IN ACCESS EXCLUSIVE MODE"
                        )
                        started = time.perf_counter()
                        response = await result(client, token, "measured-result")
                        record("http_ms", started)
                        assert (response.status_code, response.json()) == (
                            503,
                            UNAVAILABLE,
                        )
                    await store.database.drain_cleanup()
                assert all(
                    len(phases[f"sql_deadline/{name}"]) == 3
                    for name in ("sql_ms", "cleanup_ms")
                )
            assert (await result(client, token, "measured-result")).json()[
                "outcome"
            ] == "FOUND"
            assert snapshot(migrated_db) == before
        summary = {
            label: {
                "n": len(values),
                "median_ms": round(statistics.median(values), 2),
                "max_ms": round(max(values), 2),
            }
            for label, values in sorted(phases.items())
        }
        with capsys.disabled():
            print(
                "D-LAS-07 result check synthetic PostgreSQL 18.7:", json.dumps(summary)
            )

    asyncio.run(exercise())
