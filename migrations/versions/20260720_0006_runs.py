"""Persiste runs e seus itens por organização."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260720_0006"
down_revision: str | None = "20260719_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)


def _enable_tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
    )


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("offer", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "state",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "phase IN ('running', 'editing', 'awaiting', 'done', 'error')",
            name="ck_runs_phase",
        ),
        sa.CheckConstraint(
            "batch_size IS NULL OR batch_size >= 0",
            name="ck_runs_batch_size",
        ),
    )
    op.create_index(
        "ix_runs_organization_position",
        "runs",
        ["organization_id", "position"],
    )

    op.create_table(
        "run_items",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_run_items_organization_run_position",
        "run_items",
        ["organization_id", "run_id", "position"],
    )

    _enable_tenant_rls("runs")
    _enable_tenant_rls("run_items")


def downgrade() -> None:
    op.drop_index(
        "ix_run_items_organization_run_position",
        table_name="run_items",
    )
    op.drop_table("run_items")
    op.drop_index("ix_runs_organization_position", table_name="runs")
    op.drop_table("runs")
