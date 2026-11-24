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


class ResponseLost(ConnectionError):
    """Observed by the test caller instead of a confirmed HTTP response."""


async def lose_response() -> None:
    raise ResponseLost()


async def unavailable() -> None:
    raise ResultCheckUnavailable()


@asynccontextmanager
async def result_app(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    *contexts: LogisticsContext,
    fault: FaultHook = no_fault,
    result_timeout: float = 2,
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
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
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
        before, after, repeated = (seeded(migrated_db) for _ in range(3))
        same_run_client = LogisticsContext(after.run_id, CLIENT_A)
        await_faults: dict[tuple[LogisticsContext, FaultPhase, int], FaultAction] = {
            (before, "before_commit", 1): lose_response,
            (repeated, "before_commit", 1): lose_response,
            (after, "after_commit", 1): lose_response,
            (after, "before_result_read", 2): unavailable,
        }
        profiles = Profiles(await_faults)
        async with result_app(
            migrated_db,
            monkeypatch,
            before,
            after,
            repeated,
            same_run_client,
            fault=profiles,
        ) as (client, tokens, _):
            for context, token, outcome in zip(
                (before, after, repeated),
                tokens[:3],
                ("NOT_FOUND", "FOUND", "NOT_FOUND"),
                strict=True,
            ):
                with pytest.raises(ResponseLost):
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
            cursor = snapshot(migrated_db)
            failures = await asyncio.gather(
                result(client, tokens[1], "unknown"),
                result(
                    client, tokens[3], "unknown", RESULT + "?fault=before_result_read"
                ),
                result(client, tokens[0], "unknown"),
            )
            assert [(reply.status_code, reply.json()) for reply in failures] == [
                (503, UNAVAILABLE),
                (200, ABSENT),
                (200, ABSENT),
            ]
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
        async with result_app(migrated_db, monkeypatch, owner, result_timeout=3) as (
            client,
            (token,),
            store,
        ):
            before = snapshot(migrated_db)
            engine = store.database.engine
            async with engine.connect(), engine.connect():
                reply = await result(client, token, "unconfirmed-synthetic")
                assert (reply.status_code, reply.json()) == (503, UNAVAILABLE)
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
