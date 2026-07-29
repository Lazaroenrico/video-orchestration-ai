"""Add the V2 review/cancellation states and retire pending V1 test gates."""
from __future__ import annotations

from typing import Sequence

from alembic import op


revision: str = "20260728_0009"
down_revision: str | None = "20260725_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_runs_phase", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_phase",
        "runs",
        "phase IN ('running', 'editing', 'awaiting', 'review', 'done', 'error', 'cancelled')",
    )
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued', 'running', 'retry', 'succeeded', 'failed', 'cancelled')",
    )
    op.drop_constraint("ck_run_gates_status", "run_gates", type_="check")
    op.create_check_constraint(
        "ck_run_gates_status",
        "run_gates",
        "status IN ('pending', 'resolved', 'cancelled')",
    )

    op.execute(
        """
        UPDATE run_gates
        SET status = 'cancelled',
            resolution = '{"reason":"pipeline_v2_reset"}'::jsonb,
            resolved_at = CURRENT_TIMESTAMP
        WHERE status = 'pending'
        """
    )
    op.execute(
        """
        UPDATE jobs
        SET status = 'cancelled',
            error = 'pipeline_v2_reset',
            lease_expires_at = NULL,
            worker_id = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('queued', 'running', 'retry')
          AND run_id IN (
              SELECT run_id FROM run_gates WHERE status = 'cancelled'
          )
        """
    )
    op.execute(
        """
        UPDATE runs
        SET phase = 'cancelled',
            error = 'pipeline_v2_reset',
            updated_at = CURRENT_TIMESTAMP
        WHERE phase IN ('running', 'editing', 'awaiting', 'review')
          AND id IN (
              SELECT run_id FROM run_gates WHERE status = 'cancelled'
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE run_gates
        SET status = 'resolved'
        WHERE status = 'cancelled'
        """
    )
    op.execute(
        """
        UPDATE jobs
        SET status = 'failed'
        WHERE status = 'cancelled'
        """
    )
    op.execute(
        """
        UPDATE runs
        SET phase = 'error'
        WHERE phase IN ('review', 'cancelled')
        """
    )
    op.drop_constraint("ck_run_gates_status", "run_gates", type_="check")
    op.create_check_constraint(
        "ck_run_gates_status",
        "run_gates",
        "status IN ('pending', 'resolved')",
    )
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status",
        "jobs",
        "status IN ('queued', 'running', 'retry', 'succeeded', 'failed')",
    )
    op.drop_constraint("ck_runs_phase", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_phase",
        "runs",
        "phase IN ('running', 'editing', 'awaiting', 'done', 'error')",
    )
