from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import text

from app.database import Database
from app.logistics_auth import LogisticsContext

OrderStatus = Literal["ACTIVE", "TRANSFER_BLOCKED"]

READ_ORDER = text("""
    SELECT order_id, status, scheduled_delivery_date
    FROM qa.las_orders
    WHERE run_id = :run_id AND client_id = :client_id AND order_id = :order_id
""")

# Keep status and dates in one statement so they share a PostgreSQL snapshot.
# Hard-scope the join as well as the order lookup to the full composite key.
READ_OPTIONS = text("""
    SELECT orders.status, options.delivery_date
    FROM qa.las_orders AS orders
    LEFT JOIN qa.las_delivery_dates AS options
        USING (run_id, client_id, order_id)
    WHERE orders.run_id = :run_id AND orders.client_id = :client_id
        AND orders.order_id = :order_id
    ORDER BY options.delivery_date
""")


@dataclass(frozen=True)
class Order:
    order_id: str
    status: OrderStatus
    scheduled_delivery_date: date


@dataclass(frozen=True)
class DeliveryOptions:
    status: OrderStatus
    dates: list[date]


class OrderStore:
    def __init__(self, database: Database):
        self.database = database

    async def read(self, context: LogisticsContext, order_id: str) -> Order | None:
        # PostgreSQL text cannot contain NUL, including in bind parameters.
        if "\x00" in order_id:
            return None
        async with self.database.transaction() as connection:
            result = await connection.execute(
                READ_ORDER,
                {
                    "run_id": context.run_id,
                    "client_id": context.client_id,
                    "order_id": order_id,
                },
            )
            row = result.one_or_none()
        if row is None:
            return None
        return Order(row.order_id, row.status, row.scheduled_delivery_date)

    async def delivery_options(
        self, context: LogisticsContext, order_id: str
    ) -> DeliveryOptions | None:
        if "\x00" in order_id:
            return None
        async with self.database.transaction() as connection:
            result = await connection.execute(
                READ_OPTIONS,
                {
                    "run_id": context.run_id,
                    "client_id": context.client_id,
                    "order_id": order_id,
                },
            )
            rows = result.all()
        if not rows:
            return None
        return DeliveryOptions(
            rows[0].status,
            [row.delivery_date for row in rows if row.delivery_date is not None],
        )
