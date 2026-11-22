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

    async def verify() -> None:
        db = Database(Settings.from_env())
        store = RunStore(db)
        commit = AsyncTransaction.commit
        try:
            assert await store.read(uuid4()) is None
            for commit_sent in (False, True):
                uid = uuid4()
                reached_commit = asyncio.Event()
                release_commit = asyncio.Event()

                async def interrupted_commit(transaction: AsyncTransaction) -> None:
                    if commit_sent:
                        await commit(transaction)
                    reached_commit.set()
                    await release_commit.wait()

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "commit", interrupted_commit)
                    async with asyncio.TaskGroup() as group:
                        insertion = group.create_task(store.create(uid))
                        try:
                            await asyncio.wait_for(reached_commit.wait(), 3)
                            insertion.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await asyncio.wait_for(insertion, 3)
                        finally:
                            release_commit.set()

                await asyncio.wait_for(db.drain_cleanup(), 3)
                row = await asyncio.wait_for(store.read(uid), 3)
                assert (row is not None) == commit_sent
                assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            assert await asyncio.wait_for(store.create(), 3)
        finally:
            await asyncio.wait_for(db.close(), 3)

    asyncio.run(verify())
