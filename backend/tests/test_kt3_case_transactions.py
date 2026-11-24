import asyncio
import json
import statistics
import time
from datetime import date
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest
from conftest import Project
from psycopg.pq import TransactionStatus
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncTransaction,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool
from test_kt3_case_concurrency import (
    case_rows,
    post_case,
    running_app,
    wait_for_advisory_block,
)
from test_kt3_fixtures import CLIENT_B, SAME_ORDER, create_run, initialize

from app.case_store import INSERT_CASE, INSERT_KEY, OPERATION, SET_LOCK_LIMIT, TAKE_LOCK
from app.database import Database
from app.logistics_auth import LogisticsContext
from app.logistics_locks import initialization_lock, order_lock, request_lock


async def wait_for_backend_exit(project: Project, pid: int) -> None:
    async with asyncio.timeout(2):
        with project.db() as observer:
            observer.autocommit = True
            while observer.execute(
                "SELECT EXISTS (SELECT FROM pg_stat_activity WHERE pid=%s)", (pid,)
            ).fetchone() == (True,):
                await asyncio.sleep(0.02)


def test_case_lock_vectors() -> None:
    run = UUID("00112233-4455-6677-8899-aabbccddeeff")
    assert request_lock(run, "c", OPERATION, "k") == 2599177598944967164
    assert order_lock(run, "c", "o") == 5218982563760337783
    assert initialization_lock(run) == 8439567225794227074
    assert order_lock(run, "a", "bc") != order_lock(run, "ab", "c")


def test_case_isolation_and_reset(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        run = create_run(migrated_db)
        async with running_app(migrated_db, monkeypatch, run) as (_, store):
            database = store.database
            await database.engine.dispose()
            database.engine = create_async_engine(
                database.settings.url("qa_api"),
                isolation_level="REPEATABLE READ",
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
                hide_parameters=True,
            )
            options = AsyncConnection.execution_options
            visits = []

            async def verify_idle(
                connection: AsyncConnection, **kwargs: Any
            ) -> AsyncConnection:
                raw = await connection.get_raw_connection()
                driver = cast(
                    psycopg.AsyncConnection[tuple[object, ...]], raw.driver_connection
                )
                assert driver.info.transaction_status == TransactionStatus.IDLE
                assert not connection.in_transaction()
                visits.append(driver.info.backend_pid)
                return await options(connection, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execution_options", verify_idle)
                for _ in range(2):
                    async with database.transaction(case_write=True) as connection:
                        assert (
                            await connection.exec_driver_sql(
                                "SHOW transaction_isolation"
                            )
                        ).scalar_one() == "read committed"
                        pid = (
                            await connection.exec_driver_sql("SELECT pg_backend_pid()")
                        ).scalar_one()
                    async with database.engine.connect() as connection:
                        assert (
                            await connection.exec_driver_sql(
                                "SHOW transaction_isolation"
                            )
                        ).scalar_one() == "repeatable read"
                        assert (
                            await connection.exec_driver_sql("SELECT pg_backend_pid()")
                        ).scalar_one() == pid
                assert visits == [pid, pid]
            await database.engine.dispose()
            with migrated_db.db() as admin:
                admin.execute(
                    "ALTER ROLE qa_api SET session_replication_role = replica"
                )
            try:
                with pytest.raises(
                    RuntimeError, match="incompatible PostgreSQL write session"
                ):
                    async with database.transaction(case_write=True):
                        pytest.fail("replica session reached the write boundary")
            finally:
                await database.engine.dispose()
                with migrated_db.db() as admin:
                    admin.execute("ALTER ROLE qa_api RESET session_replication_role")
            async with database.transaction(case_write=True) as connection:
                assert (
                    await connection.exec_driver_sql("SHOW session_replication_role")
                ).scalar_one() == "origin"

    asyncio.run(exercise())


def test_case_rollback_and_commit_uncertainty(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        run = create_run(migrated_db)
        assert initialize(migrated_db, run)
        async with running_app(migrated_db, monkeypatch, run) as (client, store):
            db = store.database
            execute = AsyncConnection.execute
            commit = AsyncTransaction.commit
            uncertain_pids = []

            async def fail_key(
                conn: AsyncConnection, statement: Any, params: Any = None, **kw: Any
            ) -> Any:
                if statement is INSERT_KEY:
                    return await execute(conn, text("SELECT 1/0"))
                return await execute(conn, statement, params, **kw)

            before = case_rows(migrated_db, run)
            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_key)
                assert (await post_case(client, "rolled-back")).status_code == 500
                assert case_rows(migrated_db, run) == before

            async def lose_confirmation(transaction: AsyncTransaction) -> None:
                raw = await transaction.connection.get_raw_connection()
                driver = cast(
                    psycopg.AsyncConnection[tuple[object, ...]], raw.driver_connection
                )
                uncertain_pids.append(driver.info.backend_pid)
                await commit(transaction)
                cases, keys = case_rows(migrated_db, run)
                assert len(cases) == 1 and keys == [("uncertain", cases[0][0])]
                raise ConnectionError("synthetic loss of commit confirmation")

            with monkeypatch.context() as patch:
                patch.setattr(AsyncTransaction, "commit", lose_confirmation)
                response = await post_case(client, "uncertain")
                assert response.status_code == 500
                assert "commit" not in response.text
            committed = case_rows(migrated_db, run)
            assert len(committed[0]) == 1 and len(committed[1]) == 1
            assert len(uncertain_pids) == 1
            assert isinstance(uncertain_pids[0], int)
            await wait_for_backend_exit(migrated_db, uncertain_pids[0])
            assert (await post_case(client, "uncertain")).status_code == 200

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_key)
                assert (
                    await post_case(client, "duplicate-rollback")
                ).status_code == 500
                assert case_rows(migrated_db, run) == committed

            async def fail_rollback(_conn: AsyncConnection) -> None:
                raise ConnectionError("synthetic rollback transport failure")

            before_failure = case_rows(migrated_db, run)
            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", fail_key)
                patch.setattr(AsyncConnection, "rollback", fail_rollback)
                assert (
                    await post_case(client, "rollback-transport")
                ).status_code == 500
                await db.drain_cleanup()
            assert case_rows(migrated_db, run) == before_failure
            pool = cast(AsyncAdaptedQueuePool, db.engine.pool)
            assert pool.checkedout() == 0
            assert (await post_case(client, "rollback-transport")).status_code == 200

        fresh = create_run(migrated_db)
        assert initialize(migrated_db, fresh)
        before_commit = asyncio.Event()
        before_commit_pids = []

        async def pause_before_commit(transaction: AsyncTransaction) -> None:
            raw = await transaction.connection.get_raw_connection()
            driver = cast(
                psycopg.AsyncConnection[tuple[object, ...]], raw.driver_connection
            )
            before_commit_pids.append(driver.info.backend_pid)
            before_commit.set()
            await asyncio.Event().wait()

        async with running_app(migrated_db, monkeypatch, fresh) as (client, store):
            with monkeypatch.context() as patch:
                patch.setattr(AsyncTransaction, "commit", pause_before_commit)
                pending = asyncio.create_task(post_case(client, "uncommitted"))
                try:
                    async with asyncio.timeout(3):
                        await before_commit.wait()
                    assert case_rows(migrated_db, fresh) == ([], [])
                finally:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    await store.database.drain_cleanup()
            assert case_rows(migrated_db, fresh) == ([], [])
            assert len(before_commit_pids) == 1
            assert isinstance(before_commit_pids[0], int)
            await wait_for_backend_exit(migrated_db, before_commit_pids[0])
            assert (await post_case(client, "uncommitted")).status_code == 201

    asyncio.run(exercise())


def test_case_cancelled_locks_and_open_transaction(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        run = create_run(migrated_db)
        assert initialize(migrated_db, run)
        context = LogisticsContext(run, CLIENT_B)
        async with running_app(migrated_db, monkeypatch, run) as (client, store):
            db = store.database
            for lock in (
                request_lock(run, CLIENT_B, OPERATION, "cancelled"),
                order_lock(run, CLIENT_B, SAME_ORDER),
            ):
                with migrated_db.db("migrate") as holder:
                    holder.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (lock,))
                    pending = asyncio.create_task(post_case(client, "cancelled"))
                    try:
                        pid, holder_pid = await wait_for_advisory_block(migrated_db)
                        assert holder_pid == holder.info.backend_pid
                    finally:
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                        await db.drain_cleanup()
                    assert case_rows(migrated_db, run) == ([], [])
                    with migrated_db.db() as observer:
                        assert observer.execute(
                            "SELECT count(*) FROM pg_locks WHERE pid=%s", (pid,)
                        ).fetchone() == (0,)
                        assert observer.execute(
                            "SELECT pg_try_advisory_xact_lock(%s::bigint)",
                            (request_lock(run, CLIENT_B, OPERATION, "cancelled"),),
                        ).fetchone() == (
                            (
                                lock
                                != request_lock(run, CLIENT_B, OPERATION, "cancelled")
                            ),
                        )

            execute = AsyncConnection.execute
            inserted = asyncio.Event()

            async def pause_after_insert(
                conn: AsyncConnection, statement: Any, params: Any = None, **kw: Any
            ) -> Any:
                result = await execute(conn, statement, params, **kw)
                if statement is INSERT_CASE:
                    inserted.set()
                    await asyncio.Event().wait()
                return result

            with monkeypatch.context() as patch:
                patch.setattr(AsyncConnection, "execute", pause_after_insert)
                pending_write = asyncio.create_task(
                    store.create(
                        context,
                        SAME_ORDER,
                        date(2026, 11, 26),
                        "open-transaction",
                        deadline=asyncio.get_running_loop().time() + 4,
                    )
                )
                try:
                    async with asyncio.timeout(3):
                        await inserted.wait()
                    assert case_rows(migrated_db, run) == ([], [])
                finally:
                    pending_write.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending_write
                    await db.drain_cleanup()
            assert case_rows(migrated_db, run) == ([], [])
            pool = cast(AsyncAdaptedQueuePool, db.engine.pool)
            assert pool.checkedout() == 0
            async with db.engine.connect() as first, db.engine.connect() as second:
                assert (
                    await first.exec_driver_sql("SELECT pg_backend_pid()")
                ).scalar_one() != (
                    await second.exec_driver_sql("SELECT pg_backend_pid()")
                ).scalar_one()
            assert (await post_case(client, "open-transaction")).status_code == 201

    asyncio.run(exercise())


def test_case_timeout_and_phase_measurement(
    migrated_db: Project,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def exercise() -> None:
        run = create_run(migrated_db)
        assert initialize(migrated_db, run)
        phases: dict[str, list[float]] = {}
        lock_errors: list[str | None] = []
        label = "cold"

        def record(phase: str, start: float) -> None:
            phases.setdefault(f"{label}/{phase}", []).append(
                (time.perf_counter() - start) * 1000
            )

        with migrated_db.db() as observer:
            version = observer.info.server_version
        async with running_app(
            migrated_db, monkeypatch, run, statement_timeout=150
        ) as (client, store):
            db = store.database
            connect, execute, commit, release = (
                AsyncEngine.connect,
                AsyncConnection.execute,
                AsyncTransaction.commit,
                Database._release,
            )

            async def measure_connect(engine: AsyncEngine) -> AsyncConnection:
                start = time.perf_counter()
                try:
                    return await connect(engine)
                finally:
                    record("checkout_ms", start)

            async def measure_execute(
                conn: AsyncConnection, statement: Any, params: Any = None, **kw: Any
            ) -> Any:
                start = time.perf_counter()
                try:
                    if label == "lock_timeout" and statement is SET_LOCK_LIMIT:
                        params = {"budget": "60ms"}
                    return await execute(conn, statement, params, **kw)
                except DBAPIError as error:
                    if label == "lock_timeout" and statement is TAKE_LOCK:
                        lock_errors.append(getattr(error.orig, "sqlstate", None))
                    raise
                finally:
                    if statement is TAKE_LOCK:
                        record("lock_ms", start)
                    if statement is INSERT_CASE or statement is INSERT_KEY:
                        record("write_ms", start)

            async def measure_commit(tx: AsyncTransaction) -> None:
                start = time.perf_counter()
                try:
                    await commit(tx)
                finally:
                    record("commit_ms", start)

            async def measure_release(
                database: Database, conn: AsyncConnection, *, invalidate: bool = False
            ) -> None:
                start = time.perf_counter()
                try:
                    await release(database, conn, invalidate=invalidate)
                finally:
                    record("cleanup_ms", start)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncEngine, "connect", measure_connect)
                patch.setattr(AsyncConnection, "execute", measure_execute)
                patch.setattr(AsyncTransaction, "commit", measure_commit)
                patch.setattr(Database, "_release", measure_release)
                for index in range(8):
                    label = "cold" if index < 4 else "warm"
                    if label == "cold":
                        await db.engine.dispose()
                    start = time.perf_counter()
                    response = await post_case(client, f"measured-{index}")
                    record("http_total_ms", start)
                    assert response.status_code == (201 if index == 0 else 200)
                before = case_rows(migrated_db, run)
                label = "lock_timeout"
                for index in range(4):
                    with migrated_db.db("migrate") as holder:
                        holder.execute(
                            "SELECT pg_advisory_xact_lock(%s::bigint)",
                            (order_lock(run, CLIENT_B, SAME_ORDER),),
                        )
                        start = time.perf_counter()
                        response = await post_case(client, f"timeout-{index}")
                        record("http_total_ms", start)
                        assert response.status_code == 500
                    assert case_rows(migrated_db, run) == before
                assert lock_errors == ["55P03"] * 4

                # A separate test session uses the existing, longer SQL cap to observe waiters.
                async with running_app(migrated_db, patch, run) as (
                    wait_client,
                    wait_store,
                ):
                    for label in ("lock_wait_success", "cancelled"):
                        for index in range(3):
                            with migrated_db.db("migrate") as holder:
                                holder.execute(
                                    "SELECT pg_advisory_xact_lock(%s::bigint)",
                                    (order_lock(run, CLIENT_B, SAME_ORDER),),
                                )
                                start = time.perf_counter()
                                pending = asyncio.create_task(
                                    post_case(wait_client, f"{label}-{index}")
                                )
                                await wait_for_advisory_block(migrated_db)
                                if label == "cancelled":
                                    pending.cancel()
                                    with pytest.raises(asyncio.CancelledError):
                                        await pending
                                    await wait_store.database.drain_cleanup()
                            if label == "lock_wait_success":
                                assert (await pending).status_code == 200
                            record("http_total_ms", start)
                    assert all(
                        not str(key[0]).startswith("cancelled-")
                        for key in case_rows(migrated_db, run)[1]
                    )
                await db.drain_cleanup()
            assert (await post_case(client, "recovered")).status_code == 200
        out = {
            name: {
                "n": len(values),
                "median_ms": round(statistics.median(values), 2),
                "max_ms": round(max(values), 2),
            }
            for name, values in sorted(phases.items())
        }
        assert (
            version == 180007
            and {
                "lock_timeout/cleanup_ms",
                "cancelled/cleanup_ms",
                "lock_wait_success/lock_ms",
            }
            <= out.keys()
        )
        with capsys.disabled():
            print(
                "D-LAS-07 case write synthetic PostgreSQL 18.7 measurements:",
                json.dumps(out),
            )

    asyncio.run(exercise())
