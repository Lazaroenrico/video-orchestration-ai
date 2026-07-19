"""Persiste templates de prompt por organização com RLS."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260718_0002"
down_revision: str | None = "20260718_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('creator', 'video')",
            name="ck_prompt_templates_kind",
        ),
    )
    op.create_index(
        "ix_prompt_templates_organization_kind_id",
        "prompt_templates",
        ["organization_id", "kind", "id"],
    )
    op.create_table(
        "prompt_last_used",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "kind IN ('creator', 'video')",
            name="ck_prompt_last_used_kind",
        ),
    )

    for table in ("prompt_templates", "prompt_last_used"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
            f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
        )


def downgrade() -> None:
    op.drop_table("prompt_last_used")
    op.drop_index(
        "ix_prompt_templates_organization_kind_id",
        table_name="prompt_templates",
    )
    op.drop_table("prompt_templates")
