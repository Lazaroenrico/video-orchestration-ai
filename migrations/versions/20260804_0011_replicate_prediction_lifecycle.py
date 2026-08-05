"""Persist Replicate prediction lifecycle and structured error types."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0011"
down_revision: str | None = "20260729_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("error_type", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("error_type", sa.Text(), nullable=True))
    op.add_column(
        "external_effects",
        sa.Column("error_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "external_effects",
        sa.Column("provider_operation_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "external_effects",
        sa.Column("provider_status", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_external_effects_provider_status",
        "external_effects",
        "provider_status IS NULL OR provider_status IN "
        "('starting', 'processing', 'succeeded', 'failed', 'canceled')",
    )
    op.create_index(
        "uq_external_effects_provider_operation",
        "external_effects",
        ["organization_id", "provider", "provider_operation_id"],
        unique=True,
        postgresql_where=sa.text("provider_operation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_external_effects_provider_operation",
        table_name="external_effects",
    )
    op.drop_constraint(
        "ck_external_effects_provider_status",
        "external_effects",
        type_="check",
    )
    op.drop_column("external_effects", "provider_status")
    op.drop_column("external_effects", "provider_operation_id")
    op.drop_column("external_effects", "error_type")
    op.drop_column("jobs", "error_type")
    op.drop_column("runs", "error_type")
