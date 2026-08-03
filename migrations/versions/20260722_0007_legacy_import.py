"""Controla importações legadas idempotentes por tenant e origem."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260722_0007"
down_revision: str | None = "20260720_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
    )


def upgrade() -> None:
    op.create_table(
        "legacy_import_batches",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
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
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_legacy_import_batches_status",
        ),
    )
    op.create_table(
        "legacy_import_entries",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("source_id", sa.Text(), nullable=False, primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False, primary_key=True),
        sa.Column("source_key", sa.Text(), nullable=False, primary_key=True),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="applied"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_id"],
            ["legacy_import_batches.organization_id", "legacy_import_batches.source_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'failed')",
            name="ck_legacy_import_entries_status",
        ),
    )
    _secure("legacy_import_batches")
    _secure("legacy_import_entries")


def downgrade() -> None:
    op.drop_table("legacy_import_entries")
    op.drop_table("legacy_import_batches")
