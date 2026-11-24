import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Connection
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

SCHEMA = "qa"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS).replace("%", "%%"))
    return config


def expected_revision() -> str:
    heads = ScriptDirectory.from_config(migration_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError("expected exactly one packaged Alembic head")
    return heads[0]


def current_revisions(connection: Connection) -> tuple[str, ...]:
    return MigrationContext.configure(
        connection, opts={"version_table_schema": SCHEMA}
    ).get_current_heads()


@dataclass(frozen=True)
class Settings:
    password: str = field(repr=False)
    host: str = "db"
    port: int = 5432
    connect_timeout: int = 2
    statement_timeout: int = 1000
    pool_timeout: float = 1
    readiness_timeout: float = 3

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            password=os.getenv("QA_DB_PASSWORD", ""),
            host=os.getenv("QA_DB_HOST", "db"),
            port=int(os.getenv("QA_DB_PORT", "5432")),
            connect_timeout=int(os.getenv("QA_CONNECT_TIMEOUT", "2")),
            statement_timeout=int(os.getenv("QA_SQL_TIMEOUT_MS", "1000")),
            pool_timeout=float(os.getenv("QA_POOL_TIMEOUT", "1")),
            readiness_timeout=float(os.getenv("QA_READINESS_TIMEOUT", "3")),
        )
        if not (
            settings.host
            and "," not in settings.host
            and 0 < settings.port < 65536
            and settings.connect_timeout >= 2
            and settings.statement_timeout > 0
            and 0 < settings.pool_timeout < float("inf")
            and 0 < settings.readiness_timeout < float("inf")
        ):
            raise ValueError("invalid database runtime configuration")
        return settings

    def url(self, username: str) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=username,
            password=self.password,
            host=self.host,
            port=self.port,
            database="qa_demo",
        )


class CommitOutcomeUnknown(RuntimeError):
    """The API did not receive confirmation of a case commit."""


class IncompatibleRevision(RuntimeError):
    """The database does not match the packaged migration revision."""


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.head = expected_revision()
        self.engine = create_async_engine(
            settings.url("qa_api"),
            connect_args={
                "connect_timeout": settings.connect_timeout,
                "options": f"-c statement_timeout={settings.statement_timeout}",
                "application_name": "qa-api",
            },
            pool_size=2,
            max_overflow=0,
            pool_timeout=settings.pool_timeout,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        self._cleanup: set[asyncio.Task[None]] = set()
        self._invalidating: set[asyncio.Task[None]] = set()

    def _own(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._cleanup.add(task)

        def finished(resource: asyncio.Task[None]) -> None:
            self._cleanup.discard(resource)
            self._invalidating.discard(resource)
            if not resource.cancelled():
                resource.exception()

        task.add_done_callback(finished)
        return task

    @asynccontextmanager
    async def transaction(
        self, *, case_write: bool = False, read_committed: bool = False
    ) -> AsyncIterator[AsyncConnection]:
        connection = await self.engine.connect()
        cancelled = False
        invalidate = False
        try:
            if case_write or read_committed:
                await connection.execution_options(isolation_level="READ COMMITTED")
            transaction = await connection.begin()
            await self.assert_compatible(connection)
            if (
                case_write
                and (
                    await connection.exec_driver_sql("SHOW session_replication_role")
                ).scalar_one()
                != "origin"
            ):
                raise RuntimeError("incompatible PostgreSQL write session")
            yield connection
            try:
                await transaction.commit()
            except Exception:
                if not case_write:
                    raise
                invalidate = True
                raise CommitOutcomeUnknown("case commit not confirmed") from None
        except asyncio.CancelledError:
            cancelled = True
            self._invalidating.add(
                self._own(self._release(connection, invalidate=True))
            )
            raise
        finally:
            if not cancelled:
                await asyncio.shield(
                    self._own(self._release(connection, invalidate=invalidate))
                )

    async def assert_compatible(self, connection: AsyncConnection) -> None:
        if await connection.run_sync(current_revisions) != (self.head,):
            raise IncompatibleRevision("incompatible database revision")

    async def _release(
        self, connection: AsyncConnection, *, invalidate: bool = False
    ) -> None:
        try:
            if invalidate:
                await connection.invalidate()
            else:
                async with asyncio.timeout(2):
                    await connection.rollback()
        except Exception:
            await connection.invalidate()
        finally:
            await connection.close()

    @property
    def invalidating(self) -> bool:
        return any(not task.done() for task in self._invalidating)

    async def drain_cleanup(self) -> None:
        # A completed task can remain registered until its callback is scheduled.
        # Wait only for work that was pending at entry so it can never spin here.
        pending = [asyncio.shield(task) for task in self._cleanup if not task.done()]
        if pending:
            await asyncio.gather(*pending)

    async def close(self) -> None:
        await self.drain_cleanup()
        await self.engine.dispose()
