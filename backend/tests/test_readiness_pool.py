import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
import pytest
from conftest import Project
from starlette.types import Message, Scope

from app.database import Database, Settings
from app.main import app
from app.readiness import Readiness
from app.run_store import RunStore


@asynccontextmanager
async def runtime() -> AsyncIterator[tuple[Database, Readiness]]:
    db = Database(Settings.from_env())
    ready = Readiness(db)
    try:
        yield db, ready
    finally:
        await ready.close()
        await db.close()


def run(
    migrated_db: Project,
    monkeypatch: pytest.MonkeyPatch,
    check: Callable[[], Awaitable[None]],
) -> None:
    migrated_db.api_env(monkeypatch)

    async def execute() -> None:
        async with asyncio.timeout(15):
            await check()

    asyncio.run(execute())


async def recover(db: Database, ready: Readiness) -> None:
    await asyncio.wait_for(ready.drain(), 4)
    assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    assert await ready.check()
    store = RunStore(db)
    assert (await store.create(store.new_id())).status == "CREATED"
    assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]


async def unavailable(ready: Readiness) -> float:
    started = time.monotonic()
    assert not await ready.check()
    elapsed = time.monotonic() - started
    assert elapsed <= 3.5
    return elapsed


def test_connect_timeout(migrated_db: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    async def verify() -> None:
        async with runtime() as (db, ready):
            # Forward the real PostgreSQL startup packet, then withhold the server reply.
            accepted = asyncio.Event()
            opened: set[asyncio.Task[None]] = set()

            async def delay(
                client: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                peer = asyncio.current_task()
                assert peer is not None
                opened.add(peer)
                upstream: asyncio.StreamWriter | None = None
                try:
                    _, upstream = await asyncio.open_connection(migrated_db.host, 5432)
                    upstream.write(await client.read(65536))
                    await upstream.drain()
                    accepted.set()
                    await client.read()
                finally:
                    if upstream is not None:
                        upstream.close()
                    writer.close()
                    opened.discard(peer)

            listener = await asyncio.start_server(delay, "127.0.0.1", 0)
            try:
                socket = listener.sockets[0].getsockname()
                with monkeypatch.context() as patch:
                    patch.setenv("QA_DB_HOST", socket[0])
                    patch.setenv("QA_DB_PORT", str(socket[1]))
                    async with runtime() as (blocked, blocked_ready):
                        assert 1 <= await unavailable(blocked_ready)
                        assert accepted.is_set()
                        await blocked_ready.drain()
                        assert blocked.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            finally:
                listener.close()
                listener.abort_clients()
                peers = tuple(opened)
                for peer in peers:
                    peer.cancel()
                await asyncio.gather(*peers, return_exceptions=True)
                await asyncio.wait_for(listener.wait_closed(), 2)
            await recover(db, ready)

    run(migrated_db, monkeypatch, verify)


def test_sql_deadline(migrated_db: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    async def verify() -> None:
        async with runtime() as (db, ready):
            assert await ready.check()
            with migrated_db.db() as admin:
                admin.execute("LOCK TABLE qa.alembic_version IN ACCESS EXCLUSIVE MODE")
                assert 0.7 <= await unavailable(ready)
            await recover(db, ready)

    run(migrated_db, monkeypatch, verify)


def test_pool_exhaustion(migrated_db: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    async def verify() -> None:
        async with runtime() as (db, ready):
            async with db.engine.connect(), db.engine.connect():
                assert 0.7 <= await unavailable(ready)
                assert db.engine.pool.checkedout() == 2  # type: ignore[attr-defined]
                with migrated_db.db() as admin:
                    assert admin.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE usename='qa_api' AND application_name='qa-api'"
                    ).fetchone() == (2,)
            await recover(db, ready)

    run(migrated_db, monkeypatch, verify)


async def until_blocked(admin: psycopg.Connection[tuple[object, ...]]) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        admin.execute("SELECT pg_stat_clear_snapshot()")
        if admin.execute("""
            SELECT count(*) FROM pg_stat_activity
            WHERE application_name='qa-api' AND wait_event_type='Lock'
        """).fetchone() == (1,):
            return
        await asyncio.sleep(0.02)
    pytest.fail("readiness did not reach the injected PostgreSQL lock")


def test_readiness_cancellation(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify() -> None:
        async with runtime() as (db, ready):
            for two_waiters in (False, True):
                with migrated_db.db() as admin:
                    admin.execute(
                        "LOCK TABLE qa.alembic_version IN ACCESS EXCLUSIVE MODE"
                    )
                    first = asyncio.create_task(ready.check())
                    await until_blocked(admin)
                    second = asyncio.create_task(ready.check()) if two_waiters else None
                    await asyncio.sleep(0)
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    if second is None:
                        await asyncio.wait_for(ready.drain(), 3.5)
                        assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
                    else:
                        assert not second.done()
                if second is not None:
                    assert await second
                await recover(db, ready)

    run(migrated_db, monkeypatch, verify)


def test_recovers_without_connection_leaks(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify() -> None:
        async with runtime() as (db, ready):
            for _ in range(3):
                with migrated_db.db() as admin:
                    admin.execute(
                        "LOCK TABLE qa.alembic_version IN ACCESS EXCLUSIVE MODE"
                    )
                    await unavailable(ready)
                    check = asyncio.create_task(ready.check())
                    await until_blocked(admin)
                    check.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await check
                await recover(db, ready)
                with migrated_db.db() as admin:
                    total, stuck = admin.execute("""
                        SELECT count(*), count(*) FILTER (WHERE state LIKE 'idle in transaction%')
                        FROM pg_stat_activity WHERE application_name='qa-api'
                    """).fetchone() or (-1, -1)
                    assert isinstance(total, int) and 0 < total <= 2
                    assert stuck == 0

    run(migrated_db, monkeypatch, verify)


def test_asgi_disconnect_releases_connection(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify() -> None:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/health/ready",
            "raw_path": b"/health/ready",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 12345),
        }
        events: asyncio.Queue[Message] = asyncio.Queue()
        events.put_nowait({"type": "http.request", "body": b"", "more_body": False})

        async def send(message: Message) -> None:
            pass

        async with app.router.lifespan_context(app):
            ready: Readiness = app.state.readiness
            db = ready.database
            with migrated_db.db() as observer:
                observer.execute(
                    "LOCK TABLE qa.alembic_version IN ACCESS EXCLUSIVE MODE"
                )
                async with asyncio.TaskGroup() as group:
                    request = group.create_task(app(scope, events.get, send))
                    await until_blocked(observer)
                    events.put_nowait({"type": "http.disconnect"})
                    await asyncio.wait_for(request, 3)
                    await asyncio.wait_for(ready.drain(), 3)
                    assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            await recover(db, ready)

    monkeypatch.setenv("QA_SQL_TIMEOUT_MS", "5000")
    run(migrated_db, monkeypatch, verify)
