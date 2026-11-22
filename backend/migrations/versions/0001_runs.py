"""Minimal technical run record."""

import sqlalchemy as sa
from alembic import op

revision = "0001_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="runs_positive_schema_version"),
        sa.CheckConstraint("status = 'CREATED'", name="runs_created_status"),
        schema="qa",
    )
