"""Persiste creators por organização com ponteiros canônicos de mídia."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260719_0003"
down_revision: str | None = "20260718_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "creators",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("creator_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("image_uri", sa.Text(), nullable=True),
        sa.Column("voice_ref", sa.Text(), nullable=True),
        sa.Column("voice_preview_uri", sa.Text(), nullable=True),
        sa.Column(
            "angles",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("voice_reroll_count", sa.Integer(), nullable=True),
        sa.Column("creator_prompt", sa.Text(), nullable=True),
        sa.Column("video_prompt", sa.Text(), nullable=True),
        sa.Column("offer", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
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
            "status IN ('approved', 'rejected')",
            name="ck_creators_status",
        ),
    )
    op.create_index(
        "ix_creators_organization_position",
        "creators",
        ["organization_id", "position"],
    )
    op.execute("ALTER TABLE creators ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE creators FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY creators_tenant_isolation ON creators "
        f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
    )


def downgrade() -> None:
    op.drop_index("ix_creators_organization_position", table_name="creators")
    op.drop_table("creators")
