"""Add scoped delivery cases and their request keys."""

import sqlalchemy as sa
from alembic import op

revision = "0003_las_cases"
down_revision = "0002_las_fixtures"
branch_labels = None
depends_on = None

SCOPE = ("run_id", "client_id")
BUSINESS_KEY = (*SCOPE, "order_id", "requested_date", "operation_type")
CASE_REFERENCE = (*BUSINESS_KEY, "case_id")


def upgrade() -> None:
    op.create_table(
        "las_cases",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("case_id", sa.Uuid(), primary_key=True),
        sa.Column("order_id", sa.Text(collation="C"), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("operation_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            [*SCOPE, "order_id"],
            [
                "qa.las_orders.run_id",
                "qa.las_orders.client_id",
                "qa.las_orders.order_id",
            ],
            name="las_cases_order_fk",
        ),
        sa.UniqueConstraint(*BUSINESS_KEY, name="las_cases_business_key"),
        sa.UniqueConstraint(*CASE_REFERENCE, name="las_cases_reference_key"),
        sa.CheckConstraint(
            "operation_type = 'DELIVERY_RESCHEDULE'", name="las_cases_operation"
        ),
        sa.CheckConstraint("status = 'OPEN'", name="las_cases_status"),
        schema="qa",
    )
    op.create_table(
        "las_idempotency_keys",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("operation_type", sa.Text(), primary_key=True),
        sa.Column("request_key", sa.Text(collation="C"), primary_key=True),
        sa.Column("order_id", sa.Text(collation="C"), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            CASE_REFERENCE,
            [f"qa.las_cases.{column}" for column in CASE_REFERENCE],
            name="las_keys_case_fk",
        ),
        schema="qa",
    )
    op.create_index(
        "las_keys_case_idx", "las_idempotency_keys", CASE_REFERENCE, schema="qa"
    )
