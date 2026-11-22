import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import Project
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.database import Database, Settings
from app.run_store import RunStore


def test_address_two_runs(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated_db.api_env(monkeypatch)

    async def verify() -> None:
        db = Database(Settings.from_env())
        try:
            store = RunStore(db)
            first, second = await store.create(), await store.create()
            assert first.run_id.version == second.run_id.version == 4
            assert first.run_id != second.run_id
            assert await store.read(first.run_id) == first
            assert await store.read(second.run_id) == second
            assert first.created_at.utcoffset() == timedelta(0)
            assert first.schema_version == 1 and first.status == "CREATED"
            assert await store.read(uuid4()) is None
        finally:
            await db.close()

    asyncio.run(verify())
    with migrated_db.db("api") as conn:
        assert conn.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='qa' AND table_name='runs' ORDER BY ordinal_position
        """).fetchall() == [
            ("run_id",),
            ("schema_version",),
            ("created_at",),
            ("status",),
        ]


def test_commit_visibility(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated_db.api_env(monkeypatch)

    async def verify() -> None:
        db = Database(Settings.from_env())
        transactions: list[str] = []
        try:
            event.listen(
                db.engine.sync_engine,
                "begin",
                lambda conn: transactions.append("begin"),
            )
            event.listen(
                db.engine.sync_engine,
                "commit",
                lambda conn: transactions.append("commit"),
            )
            row = await RunStore(db).create()
            assert transactions == ["begin", "commit"]
            with migrated_db.db("api") as other:
                assert other.execute("SELECT * FROM qa.runs").fetchone() == (
                    row.run_id,
                    row.schema_version,
                    row.created_at,
                    row.status,
                )
        finally:
            await db.close()

    asyncio.run(verify())


def test_duplicate_run_id(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated_db.api_env(monkeypatch)

    async def verify() -> None:
        db = Database(Settings.from_env())
        try:
            store = RunStore(db)
            existing = await store.create()
            with pytest.raises(IntegrityError):
                await store.create(existing.run_id)
            assert await store.read(existing.run_id) == existing
        finally:
            await db.close()

    asyncio.run(verify())
    with migrated_db.db("api") as conn:
        assert conn.execute("SELECT count(*) FROM qa.runs").fetchone() == (1,)


def test_revision_gate_before_storage(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated_db.api_env(monkeypatch)
    with migrated_db.db() as admin:
        admin.execute("DELETE FROM qa.alembic_version")

    async def verify() -> None:
        db = Database(Settings.from_env())
        statements: list[str] = []

        @event.listens_for(db.engine.sync_engine, "before_cursor_execute")
        def trace(conn: object, cursor: object, sql: str, *args: object) -> None:
            statements.append(sql)

        try:
            store = RunStore(db)
            for operation in ("create", "read"):
                with pytest.raises(
                    RuntimeError, match="incompatible database revision"
                ):
                    if operation == "create":
                        await store.create()
                    else:
                        await store.read(uuid4())
            assert any("alembic_version" in statement for statement in statements)
            assert not any("qa.runs" in statement for statement in statements)
        finally:
            await db.close()

    asyncio.run(verify())


def test_cancelled_write_recovery(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncTransaction

    migrated_db.api_env(monkeypatch)
    monkeypatch.setenv("QA_SQL_TIMEOUT_MS", "5000")

    async def verify() -> None:
        db = Database(Settings.from_env())
        store = RunStore(db)
        original_commit = AsyncTransaction.commit
        try:
            cancelled_id = uuid4()
            with migrated_db.db() as observer:
                observer.execute("LOCK TABLE qa.runs IN ACCESS EXCLUSIVE MODE")
                async with asyncio.TaskGroup() as group:
                    insertion = group.create_task(store.create(cancelled_id))
                    try:
                        async with asyncio.timeout(3):
                            while True:
                                observer.execute("SELECT pg_stat_clear_snapshot()")
                                blocked = observer.execute("""
                                    SELECT count(*) FROM pg_stat_activity
                                    WHERE application_name = 'qa-api'
                                      AND wait_event_type = 'Lock'
                                      AND query LIKE 'INSERT INTO qa.runs%'
                                """).fetchone()
                                if blocked == (1,):
                                    break
                                await asyncio.sleep(0.02)
                    finally:
                        insertion.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(insertion, 3)

            await asyncio.wait_for(db.drain_cleanup(), 3)
            assert await asyncio.wait_for(store.read(cancelled_id), 3) is None

            async def lose_commit_result(transaction: AsyncTransaction) -> None:
                await original_commit(transaction)
                raise asyncio.CancelledError(
                    "injected interruption after real PostgreSQL commit"
                )

            possibly_committed = uuid4()
            with monkeypatch.context() as patch:
                patch.setattr(AsyncTransaction, "commit", lose_commit_result)
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(store.create(possibly_committed), 3)

            await asyncio.wait_for(db.drain_cleanup(), 3)
            persisted = await asyncio.wait_for(store.read(possibly_committed), 3)
            assert persisted is not None and persisted.run_id == possibly_committed
            assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            assert await asyncio.wait_for(store.create(), 3)
        finally:
            await asyncio.wait_for(db.close(), 3)

    asyncio.run(verify())
