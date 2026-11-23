"""Add scoped synthetic clients, orders and delivery options."""

import sqlalchemy as sa
from alembic import op

revision = "0002_las_fixtures"
down_revision = "0001_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "las_fixture_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("fixture_version", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["qa.runs.run_id"], name="las_fixture_runs_run_fk"
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="las_fixture_runs_sha256"
        ),
        schema="qa",
    )
    op.create_table(
        "las_run_clients",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Text(collation="C"), primary_key=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["qa.las_fixture_runs.run_id"], name="las_run_clients_run_fk"
        ),
        schema="qa",
    )
    op.create_table(
        "las_orders",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("order_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scheduled_delivery_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "client_id"],
            ["qa.las_run_clients.run_id", "qa.las_run_clients.client_id"],
            name="las_orders_client_fk",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'TRANSFER_BLOCKED')", name="las_orders_status"
        ),
        schema="qa",
    )
    op.create_table(
        "las_delivery_dates",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("order_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("delivery_date", sa.Date(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["run_id", "client_id", "order_id"],
            [
                "qa.las_orders.run_id",
                "qa.las_orders.client_id",
                "qa.las_orders.order_id",
            ],
            name="las_delivery_dates_order_fk",
        ),
        schema="qa",
    )
