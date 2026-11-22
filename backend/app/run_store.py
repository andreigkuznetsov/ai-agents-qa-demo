from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    insert,
    select,
)
from sqlalchemy.engine import Row

from app.database import SCHEMA, Database

runs = Table(
    "runs",
    MetaData(),
    Column("run_id", Uuid(), primary_key=True),
    Column("schema_version", Integer(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("status", Text(), nullable=False),
    schema=SCHEMA,
)


@dataclass(frozen=True)
class Run:
    run_id: UUID
    schema_version: int
    created_at: datetime
    status: str

    @classmethod
    def from_row(cls, row: Row[UUID, int, datetime, str]) -> "Run":
        uid, version, created, status = row
        return cls(uid, version, created.astimezone(UTC), status)


class RunStore:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, run_id: UUID | None = None) -> Run:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                insert(runs)
                .values(
                    run_id=run_id if run_id is not None else uuid4(),
                    schema_version=1,
                    status="CREATED",
                )
                .returning(*runs.c)
            )
            row = Run.from_row(result.one())
        return row

    async def read(self, run_id: UUID) -> Run | None:
        async with self.database.transaction() as connection:
            result = await connection.execute(
                select(runs).where(runs.c.run_id == run_id)
            )
            row = result.one_or_none()
            return Run.from_row(row) if row is not None else None
