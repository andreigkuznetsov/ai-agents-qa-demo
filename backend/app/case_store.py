import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Row

from app.database import Database
from app.logistics_auth import LogisticsContext
from app.logistics_locks import order_lock, request_lock

Operation = Literal["DELIVERY_RESCHEDULE"]
Rejection = Literal[
    "RESOURCE_NOT_FOUND",
    "ORDER_TRANSFER_BLOCKED",
    "DELIVERY_DATE_NOT_ALLOWED",
    "IDEMPOTENCY_KEY_CONFLICT",
]
OPERATION: Operation = "DELIVERY_RESCHEDULE"
FIELDS = "case_id, order_id, requested_date, operation_type, status"
SCOPE = "run_id = :run_id AND client_id = :client_id"
ORDER_SCOPE = f"{SCOPE} AND order_id = :order_id"
ORDER_EXISTS = text(f"SELECT 1 FROM qa.las_orders WHERE {ORDER_SCOPE}")
FIND_KEY = text(f"""
    SELECT {FIELDS} FROM qa.las_idempotency_keys
    JOIN qa.las_cases USING (run_id, client_id, case_id, order_id, requested_date, operation_type)
    WHERE {SCOPE} AND operation_type = :operation_type AND request_key = :request_key
""")
CHECK_CONDITIONS = text(f"""
    SELECT status, EXISTS (
        SELECT 1 FROM qa.las_delivery_dates WHERE {ORDER_SCOPE} AND delivery_date = :requested_date
    ) AS date_allowed
    FROM qa.las_orders WHERE {ORDER_SCOPE}
""")
FIND_DUPLICATE = text(f"""
    SELECT {FIELDS} FROM qa.las_cases WHERE {ORDER_SCOPE}
      AND requested_date = :requested_date AND operation_type = :operation_type
""")
INSERT_CASE = text(f"""
    INSERT INTO qa.las_cases (run_id, client_id, case_id, order_id, requested_date, operation_type, status)
    VALUES (:run_id, :client_id, :case_id, :order_id, :requested_date, :operation_type, 'OPEN')
    RETURNING {FIELDS}
""")
INSERT_KEY = text("""
    INSERT INTO qa.las_idempotency_keys (run_id, client_id, operation_type, request_key, order_id, requested_date, case_id)
    VALUES (:run_id, :client_id, :operation_type, :request_key, :order_id, :requested_date, :case_id)
""")
READ_CASE = text(
    f"SELECT {FIELDS} FROM qa.las_cases WHERE {SCOPE} AND case_id = :case_id"
)
READ_CASES = text(f"""
    SELECT case_id, order_id, requested_date, operation_type, cases.status
    FROM qa.las_orders
    LEFT JOIN qa.las_cases AS cases USING (run_id, client_id, order_id)
    WHERE {ORDER_SCOPE}
    ORDER BY case_id
""")
TAKE_LOCK = text("SELECT pg_advisory_xact_lock(CAST(:lock AS bigint))")
SET_LOCK_LIMIT = text("SELECT set_config('lock_timeout', :budget, true)")


@dataclass(frozen=True)
class Case:
    case_id: UUID
    order_id: str
    requested_date: date
    operation_type: Operation
    status: Literal["OPEN"]

    @classmethod
    def from_row(cls, row: Row[UUID, str, date, Operation, Literal["OPEN"]]) -> "Case":
        return cls(*row)


class CaseRejected(Exception):
    def __init__(self, code: Rejection):
        super().__init__(code)
        self.code = code


class CaseStore:
    def __init__(self, database: Database):
        self.database = database

    async def create(
        self,
        context: LogisticsContext,
        order_id: str,
        requested_date: date,
        key: str,
        *,
        deadline: float,
    ) -> tuple[Case, bool]:
        params = {
            "run_id": context.run_id,
            "client_id": context.client_id,
            "order_id": order_id,
            "requested_date": requested_date,
            "operation_type": OPERATION,
            "request_key": key,
        }
        loop = asyncio.get_running_loop()
        async with (
            asyncio.timeout_at(deadline),
            self.database.transaction(case_write=True) as conn,
        ):
            if (await conn.execute(ORDER_EXISTS, params)).scalar_one_or_none() is None:
                raise CaseRejected("RESOURCE_NOT_FOUND")
            for lock in (
                request_lock(context.run_id, context.client_id, OPERATION, key),
                order_lock(context.run_id, context.client_id, order_id),
            ):
                remaining = int((deadline - loop.time()) * 1000)
                if remaining < 1:
                    raise TimeoutError("case write deadline expired")
                limit = min(remaining, self.database.settings.statement_timeout)
                await conn.execute(SET_LOCK_LIMIT, {"budget": f"{limit}ms"})
                await conn.execute(TAKE_LOCK, {"lock": lock})

            if (await conn.execute(ORDER_EXISTS, params)).scalar_one_or_none() is None:
                raise CaseRejected("RESOURCE_NOT_FOUND")
            saved = (await conn.execute(FIND_KEY, params)).one_or_none()
            if saved is not None:
                if saved.order_id != order_id or saved.requested_date != requested_date:
                    raise CaseRejected("IDEMPOTENCY_KEY_CONFLICT")
                return Case.from_row(saved), False

            conditions = (await conn.execute(CHECK_CONDITIONS, params)).one_or_none()
            if conditions is None:
                raise CaseRejected("RESOURCE_NOT_FOUND")
            if conditions.status != "ACTIVE":
                raise CaseRejected("ORDER_TRANSFER_BLOCKED")
            if not conditions.date_allowed:
                raise CaseRejected("DELIVERY_DATE_NOT_ALLOWED")
            case_row = (await conn.execute(FIND_DUPLICATE, params)).one_or_none()
            created = case_row is None
            if case_row is None:
                case_row = (
                    await conn.execute(INSERT_CASE, {**params, "case_id": uuid4()})
                ).one()
            case = Case.from_row(case_row)
            await conn.execute(INSERT_KEY, {**params, "case_id": case.case_id})
        return case, created

    async def read(self, context: LogisticsContext, case_id: UUID) -> Case | None:
        async with self.database.transaction() as conn:
            result = await conn.execute(
                READ_CASE,
                {
                    "run_id": context.run_id,
                    "client_id": context.client_id,
                    "case_id": case_id,
                },
            )
            row = result.one_or_none()
        return Case.from_row(row) if row is not None else None

    async def list_for_order(
        self, context: LogisticsContext, order_id: str
    ) -> list[Case] | None:
        if "\x00" in order_id:
            return None
        async with self.database.transaction() as conn:
            result = await conn.execute(
                READ_CASES,
                {
                    "run_id": context.run_id,
                    "client_id": context.client_id,
                    "order_id": order_id,
                },
            )
            rows = result.all()
        if not rows:
            return None
        return [Case.from_row(row) for row in rows if row.case_id is not None]
