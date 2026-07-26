"""Cria jobs, eventos e outbox duráveis por tenant."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260725_0008"
down_revision: str | None = "20260722_0007"
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
        "jobs",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry', 'succeeded', 'failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        sa.CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts"),
    )
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["organization_id", "status", "available_at", "created_at"],
    )

    op.create_table(
        "run_gates",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("gate_type", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("resolution", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "run_id",
            "gate_type",
            "version",
            name="uq_run_gates_version",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_run_gates_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_run_gates_version"),
    )
    op.create_index(
        "ix_run_gates_pending",
        "run_gates",
        ["organization_id", "run_id", "status", "version"],
    )

    op.create_table(
        "run_events",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
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
        "ix_run_events_replay",
        "run_events",
        ["organization_id", "run_id", "seq"],
    )

    op.create_table(
        "outbox",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("message_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="ck_outbox_status",
        ),
    )
    op.create_index(
        "ix_outbox_delivery",
        "outbox",
        ["organization_id", "status", "available_at", "id"],
    )

    op.create_table(
        "provider_quotas",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("provider", sa.Text(), nullable=False, primary_key=True),
        sa.Column("limit_units", sa.BigInteger(), nullable=False),
        sa.Column(
            "used_units",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("limit_units >= 0", name="ck_provider_quotas_limit"),
        sa.CheckConstraint(
            "used_units >= 0 AND used_units <= limit_units",
            name="ck_provider_quotas_usage",
        ),
    )

    op.create_table(
        "external_effects",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("effect_key", sa.Text(), nullable=False, primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("units > 0", name="ck_external_effects_units"),
        sa.CheckConstraint(
            "status IN ('reserved', 'succeeded', 'uncertain', 'failed')",
            name="ck_external_effects_status",
        ),
    )
    op.create_index(
        "ix_external_effects_run",
        "external_effects",
        ["organization_id", "run_id", "created_at"],
    )

    for table in (
        "jobs",
        "run_gates",
        "run_events",
        "outbox",
        "provider_quotas",
        "external_effects",
    ):
        _secure(table)


def downgrade() -> None:
    op.drop_index("ix_external_effects_run", table_name="external_effects")
    op.drop_table("external_effects")
    op.drop_table("provider_quotas")
    op.drop_index("ix_outbox_delivery", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_run_events_replay", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_run_gates_pending", table_name="run_gates")
    op.drop_table("run_gates")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
