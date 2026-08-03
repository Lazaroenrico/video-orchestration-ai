"""Persiste feedback agregado por run e organização."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260719_0004"
down_revision: str | None = "20260719_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "run_feedback",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
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
    )
    op.create_index(
        "ix_run_feedback_organization_position",
        "run_feedback",
        ["organization_id", "position"],
    )
    op.execute("ALTER TABLE run_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE run_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY run_feedback_tenant_isolation ON run_feedback "
        f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_feedback_organization_position",
        table_name="run_feedback",
    )
    op.drop_table("run_feedback")
