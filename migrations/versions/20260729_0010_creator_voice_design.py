"""Add creator voice design columns and status constraints."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_0010"
down_revision: str | None = "20260728_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "creators",
        sa.Column(
            "voice_spec",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("creators", sa.Column("voice_provider", sa.String(), nullable=True))
    op.add_column("creators", sa.Column("voice_design_model", sa.String(), nullable=True))
    op.add_column("creators", sa.Column("voice_tts_model", sa.String(), nullable=True))
    op.add_column("creators", sa.Column("voice_design_hash", sa.String(), nullable=True))
    op.add_column("creators", sa.Column("voice_selected_candidate", sa.String(), nullable=True))
    op.add_column(
        "creators",
        sa.Column(
            "voice_status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.add_column(
        "creators",
        sa.Column(
            "voice_design_meta",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_creators_voice_status",
        "creators",
        "voice_status IN ('legacy', 'candidates_ready', 'selected', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_creators_voice_status", "creators", type_="check")
    op.drop_column("creators", "voice_design_meta")
    op.drop_column("creators", "voice_status")
    op.drop_column("creators", "voice_selected_candidate")
    op.drop_column("creators", "voice_design_hash")
    op.drop_column("creators", "voice_tts_model")
    op.drop_column("creators", "voice_design_model")
    op.drop_column("creators", "voice_provider")
    op.drop_column("creators", "voice_spec")
