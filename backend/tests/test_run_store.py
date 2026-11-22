import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import Project
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.database import Database, Settings
from app.run_store import RunStore


def test_address_two_runs(migrated: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    migrated.api_env(monkeypatch)

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
    with migrated.db("api") as conn:
        assert conn.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='qa' AND table_name='runs' ORDER BY ordinal_position
        """).fetchall() == [
            ("run_id",),
            ("schema_version",),
            ("created_at",),
            ("status",),
        ]


def test_commit_visibility(migrated: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    migrated.api_env(monkeypatch)

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
            with migrated.db("api") as other:
                assert other.execute("SELECT * FROM qa.runs").fetchone() == (
                    row.run_id,
                    row.schema_version,
                    row.created_at,
                    row.status,
                )
        finally:
            await db.close()

    asyncio.run(verify())


def test_duplicate_run_id(migrated: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    migrated.api_env(monkeypatch)

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
    with migrated.db("api") as conn:
        assert conn.execute("SELECT count(*) FROM qa.runs").fetchone() == (1,)


def test_revision_gate_before_storage(
    migrated: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated.api_env(monkeypatch)
    with migrated.db() as admin:
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
    migrated: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrated.api_env(monkeypatch)
    from sqlalchemy.ext.asyncio import AsyncTransaction

    async def verify() -> None:
        db = Database(Settings.from_env())
        store = RunStore(db)
        actual_commit = AsyncTransaction.commit
        try:
            for commit_first in (False, True):
                waiting = asyncio.Event()

                async def pause(
                    tx: AsyncTransaction, already_committed: bool = commit_first
                ) -> None:
                    if already_committed:
                        await actual_commit(tx)
                    waiting.set()
                    await asyncio.Future[None]()

                uid = uuid4()
                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "commit", pause)
                    task = asyncio.create_task(store.create(uid))
                    await asyncio.wait_for(waiting.wait(), 3)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                await db.drain_cleanup()
                row = await store.read(uid)
                assert (row is not None) == commit_first
                assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            assert await store.create()
        finally:
            await db.close()

    asyncio.run(verify())
