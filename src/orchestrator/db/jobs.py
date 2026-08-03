"""Fila de execução, eventos e outbox PostgreSQL tenant-scoped."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import (
    Job as JobModel,
)
from orchestrator.db.models import (
    Outbox as OutboxModel,
)
from orchestrator.db.models import (
    Run as RunModel,
)
from orchestrator.db.models import (
    RunEvent as RunEventModel,
)
from orchestrator.db.models import (
    RunGate as RunGateModel,
)
from orchestrator.db.tenancy import TenantContext

LEASE_SECONDS = 120
OUTBOX_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Job:
    job_id: UUID
    run_id: str
    kind: str
    status: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    worker_id: str | None
    error: str | None


@dataclass(frozen=True)
class RunEvent:
    seq: int
    run_id: str
    event_type: str
    data: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class OutboxEntry:
    entry_id: int
    topic: str
    message_key: str
    payload: dict[str, Any]
    status: str
    attempt: int
    lease_expires_at: datetime | None
    worker_id: str | None
    error: str | None


@dataclass(frozen=True)
class RunGate:
    gate_id: UUID
    run_id: str
    gate_type: str
    version: int
    status: str
    payload: dict[str, Any]
    resolution: dict[str, Any] | None


class LeaseLostError(RuntimeError):
    """O worker não possui mais o lease do job."""


class StaleGateError(RuntimeError):
    """O gate já foi resolvido ou a versão enviada não é a atual."""


class CancelledGateError(RuntimeError):
    """The requested gate was intentionally cancelled."""


@dataclass(frozen=True)
class CancellationSummary:
    gates: int
    runs: int
    jobs: int


def _job_id(organization_id: UUID, run_id: str, kind: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"orchestrator:{organization_id}:{run_id}:{kind}",
    )


def _job(row: tuple[Any, ...]) -> Job:
    return Job(
        job_id=row[0],
        run_id=row[1],
        kind=row[2],
        status=row[3],
        payload=row[4],
        attempt=row[5],
        max_attempts=row[6],
        available_at=row[7],
        lease_expires_at=row[8],
        worker_id=row[9],
        error=row[10],
    )


_JOB_COLUMNS = (
    JobModel.id,
    JobModel.run_id,
    JobModel.kind,
    JobModel.status,
    JobModel.payload,
    JobModel.attempt,
    JobModel.max_attempts,
    JobModel.available_at,
    JobModel.lease_expires_at,
    JobModel.worker_id,
    JobModel.error,
)


class PostgresJobRepository:
    """Mantém a máquina de jobs e seu log público numa única fronteira."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def enqueue_run(
        self,
        run_id: str,
        *,
        offer: str,
        platform: str,
        batch_size: int,
        payload: dict[str, Any],
        max_attempts: int = 5,
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or datetime.now(UTC)
        job_id = _job_id(self._tenant.organization_id, run_id, "execute_run")
        async with self._database.connection(self._tenant) as connection:
            stmt_run = (
                pg_insert(RunModel)
                .values(
                    organization_id=self._tenant.organization_id,
                    id=run_id,
                    offer=offer,
                    platform=platform,
                    batch_size=batch_size,
                    phase="running",
                    summary={},
                    state={},
                )
                .on_conflict_do_nothing(index_elements=["organization_id", "id"])
            )
            await self._database.execute(connection,stmt_run)

            stmt_job = (
                pg_insert(JobModel)
                .values(
                    organization_id=self._tenant.organization_id,
                    id=job_id,
                    run_id=run_id,
                    kind="execute_run",
                    status="queued",
                    payload=payload,
                    max_attempts=max_attempts,
                    available_at=timestamp,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                .on_conflict_do_nothing(index_elements=["organization_id", "id"])
                .returning(JobModel.id)
            )
            inserted = await self._database.execute(connection,stmt_job)
            if await inserted.fetchone() is not None:
                await self._append_event(
                    connection,
                    run_id,
                    "run_queued",
                    {"job_id": str(job_id)},
                    timestamp,
                )
                await self._append_event(
                    connection,
                    run_id,
                    "run_start",
                    {
                        "run_id": run_id,
                        "offer": offer,
                        "batch": batch_size,
                    },
                    timestamp,
                )
                stmt_outbox = pg_insert(OutboxModel).values(
                    organization_id=self._tenant.organization_id,
                    topic="run.queued",
                    message_key=str(job_id),
                    payload={"job_id": str(job_id), "run_id": run_id},
                    available_at=timestamp,
                    created_at=timestamp,
                )
                await self._database.execute(connection,stmt_outbox)

            stmt_select = (
                select(*_JOB_COLUMNS)
                .where(
                    JobModel.organization_id == self._tenant.organization_id,
                    JobModel.id == job_id,
                )
            )
            cursor = await self._database.execute(connection,stmt_select)
            row = await cursor.fetchone()
        assert row is not None
        return _job(row)

    async def claim(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        now: datetime | None = None,
    ) -> list[Job]:
        timestamp = now or datetime.now(UTC)
        candidates = (
            select(JobModel.id)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                or_(
                    and_(
                        JobModel.status.in_(["queued", "retry"]),
                        JobModel.available_at <= timestamp,
                    ),
                    and_(
                        JobModel.status == "running",
                        JobModel.lease_expires_at <= timestamp,
                    ),
                ),
            )
            .order_by(JobModel.available_at, JobModel.created_at, JobModel.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
            .cte("candidates")
        )

        stmt = (
            update(JobModel)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.id == candidates.c.id,
            )
            .values(
                status="running",
                attempt=JobModel.attempt + 1,
                worker_id=worker_id,
                lease_expires_at=timestamp + timedelta(seconds=LEASE_SECONDS),
                error=None,
                updated_at=timestamp,
            )
            .returning(*_JOB_COLUMNS)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            rows = await cursor.fetchall()
            jobs = [_job(row) for row in rows]
            for claimed in jobs:
                await self._append_event(
                    connection,
                    claimed.run_id,
                    "job_started",
                    {
                        "job_id": str(claimed.job_id),
                        "attempt": claimed.attempt,
                        "worker_id": worker_id,
                    },
                    timestamp,
                )
        return jobs

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        stmt = (
            update(JobModel)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.id == job_id,
                JobModel.status == "running",
                JobModel.worker_id == worker_id,
                JobModel.lease_expires_at > timestamp,
            )
            .values(
                status="succeeded",
                lease_expires_at=None,
                updated_at=timestamp,
            )
            .returning(JobModel.run_id)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLostError(f"lease perdido para job {job_id}")
            await self._append_event(
                connection,
                row[0],
                "job_succeeded",
                {"job_id": str(job_id)},
                timestamp,
            )

    async def renew(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or datetime.now(UTC)
        stmt = (
            update(JobModel)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.id == job_id,
                JobModel.status == "running",
                JobModel.worker_id == worker_id,
                JobModel.lease_expires_at > timestamp,
            )
            .values(
                lease_expires_at=timestamp + timedelta(seconds=LEASE_SECONDS),
                updated_at=timestamp,
            )
            .returning(*_JOB_COLUMNS)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLostError(f"lease perdido para job {job_id}")
            renewed = _job(row)
            await self._append_event(
                connection,
                renewed.run_id,
                "job_lease_renewed",
                {"job_id": str(job_id), "worker_id": worker_id},
                timestamp,
            )
        return renewed

    async def fail(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        error: str,
        retryable: bool = True,
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or datetime.now(UTC)
        stmt_check = (
            select(*_JOB_COLUMNS)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.id == job_id,
                JobModel.status == "running",
                JobModel.worker_id == worker_id,
                JobModel.lease_expires_at > timestamp,
            )
            .with_for_update()
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt_check)
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLostError(f"lease perdido para job {job_id}")
            current = _job(row)
            will_retry = retryable and current.attempt < current.max_attempts
            delay_seconds = min(5 * (2 ** (current.attempt - 1)), 300)

            stmt_update = (
                update(JobModel)
                .where(
                    JobModel.organization_id == self._tenant.organization_id,
                    JobModel.id == job_id,
                )
                .values(
                    status="retry" if will_retry else "failed",
                    available_at=timestamp + timedelta(seconds=delay_seconds if will_retry else 0),
                    lease_expires_at=None,
                    worker_id=None,
                    error=error[:2000],
                    updated_at=timestamp,
                )
                .returning(*_JOB_COLUMNS)
            )
            updated = await self._database.execute(connection,stmt_update)
            updated_row = await updated.fetchone()
            assert updated_row is not None
            failed = _job(updated_row)
            event_type = "job_retry" if will_retry else "job_failed"
            await self._append_event(
                connection,
                failed.run_id,
                event_type,
                {
                    "job_id": str(job_id),
                    "attempt": failed.attempt,
                    "error": failed.error,
                },
                timestamp,
            )
            if not will_retry:
                stmt_run_error = (
                    update(RunModel)
                    .where(
                        RunModel.organization_id == self._tenant.organization_id,
                        RunModel.id == failed.run_id,
                    )
                    .values(
                        phase="error",
                        error=failed.error,
                        updated_at=timestamp,
                    )
                )
                await self._database.execute(connection,stmt_run_error)
                await self._append_event(
                    connection,
                    failed.run_id,
                    "error",
                    {"message": failed.error or "job failed"},
                    timestamp,
                )
        return failed

    async def get(self, job_id: UUID) -> Job | None:
        stmt = (
            select(*_JOB_COLUMNS)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.id == job_id,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            row = await cursor.fetchone()
        return _job(row) if row is not None else None

    async def get_initial_run_payload(self, run_id: str) -> dict[str, Any] | None:
        stmt = (
            select(JobModel.payload)
            .where(
                JobModel.organization_id == self._tenant.organization_id,
                JobModel.run_id == run_id,
                JobModel.kind == "execute_run",
            )
            .order_by(JobModel.created_at)
            .limit(1)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            row = await cursor.fetchone()
        if row is None or not isinstance(row[0], dict):
            return None
        return row[0]

    async def open_gate(
        self,
        run_id: str,
        *,
        gate_type: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> RunGate:
        timestamp = now or datetime.now(UTC)
        stmt_run = (
            select(RunModel.id)
            .where(
                RunModel.organization_id == self._tenant.organization_id,
                RunModel.id == run_id,
            )
            .with_for_update()
        )
        stmt_version = (
            select(func.coalesce(func.max(RunGateModel.version), 0) + 1)
            .where(
                RunGateModel.organization_id == self._tenant.organization_id,
                RunGateModel.run_id == run_id,
                RunGateModel.gate_type == gate_type,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            locked_run = await self._database.execute(connection,stmt_run)
            if await locked_run.fetchone() is None:
                raise ValueError(f"run {run_id!r} inexistente")
            versions = await self._database.execute(connection,stmt_version)
            version = int((await versions.fetchone())[0])
            gate_id = _job_id(
                self._tenant.organization_id,
                run_id,
                f"gate:{gate_type}:{version}",
            )
            stmt_insert_gate = (
                pg_insert(RunGateModel)
                .values(
                    organization_id=self._tenant.organization_id,
                    id=gate_id,
                    run_id=run_id,
                    gate_type=gate_type,
                    version=version,
                    status="pending",
                    payload=payload,
                    created_at=timestamp,
                )
            )
            stmt_update_run = (
                update(RunModel)
                .where(
                    RunModel.organization_id == self._tenant.organization_id,
                    RunModel.id == run_id,
                )
                .values(
                    phase=(
                        "editing"
                        if gate_type == "edit_concepts"
                        else "review"
                        if gate_type == "review_creative_plan"
                        else "awaiting"
                    ),
                    updated_at=timestamp,
                )
            )
            await self._database.execute(connection,stmt_insert_gate)
            await self._database.execute(connection,stmt_update_run)
            await self._append_event(
                connection,
                run_id,
                "gate_opened",
                {
                    "gate_id": str(gate_id),
                    "gate_type": gate_type,
                    "version": version,
                    "payload": payload,
                },
                timestamp,
            )
            public_type = {
                "edit_concepts": "awaiting_concept_edit",
                "review_creative_plan": "awaiting_review",
            }.get(gate_type, "awaiting_approval")
            public_data = {
                "run_id": run_id,
                "gate_id": str(gate_id),
                "version": version,
                "gate_type": gate_type,
                **{key: value for key, value in payload.items() if key != "type"},
            }
            await self._append_event(
                connection,
                run_id,
                public_type,
                public_data,
                timestamp,
            )
        return RunGate(
            gate_id=gate_id,
            run_id=run_id,
            gate_type=gate_type,
            version=version,
            status="pending",
            payload=payload,
            resolution=None,
        )

    async def get_pending_gate(self, run_id: str) -> RunGate | None:
        stmt = (
            select(
                RunGateModel.id,
                RunGateModel.run_id,
                RunGateModel.gate_type,
                RunGateModel.version,
                RunGateModel.status,
                RunGateModel.payload,
                RunGateModel.resolution,
            )
            .where(
                RunGateModel.organization_id == self._tenant.organization_id,
                RunGateModel.run_id == run_id,
                RunGateModel.status == "pending",
            )
            .order_by(RunGateModel.version.desc())
            .limit(1)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            row = await cursor.fetchone()
        return RunGate(*row) if row is not None else None

    async def resolve_gate(
        self,
        gate_id: UUID,
        *,
        version: int,
        resolution: dict[str, Any],
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or datetime.now(UTC)
        stmt_gate = (
            update(RunGateModel)
            .where(
                RunGateModel.organization_id == self._tenant.organization_id,
                RunGateModel.id == gate_id,
                RunGateModel.version == version,
                RunGateModel.status == "pending",
            )
            .values(
                status="resolved",
                resolution=resolution,
                resolved_at=timestamp,
            )
            .returning(
                RunGateModel.run_id,
                RunGateModel.gate_type,
                RunGateModel.payload,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt_gate)
            gate_row = await cursor.fetchone()
            if gate_row is None:
                status_cursor = await self._database.execute(
                    connection,
                    select(RunGateModel.status).where(
                        RunGateModel.organization_id
                        == self._tenant.organization_id,
                        RunGateModel.id == gate_id,
                        RunGateModel.version == version,
                    ),
                )
                status_row = await status_cursor.fetchone()
                if status_row is not None and status_row[0] == "cancelled":
                    raise CancelledGateError(
                        f"gate {gate_id} versão {version} foi cancelado"
                    )
                raise StaleGateError(
                    f"gate {gate_id} versão {version} está stale"
                )
            run_id, gate_type, gate_payload = gate_row

            stmt_initial_job = (
                select(JobModel.payload)
                .where(
                    JobModel.organization_id == self._tenant.organization_id,
                    JobModel.run_id == run_id,
                    JobModel.kind == "execute_run",
                )
                .order_by(JobModel.created_at)
                .limit(1)
            )
            initial_cursor = await self._database.execute(connection,stmt_initial_job)
            initial_row = await initial_cursor.fetchone()

            payload = {
                "gate_id": str(gate_id),
                "gate_version": version,
                "gate_type": gate_type,
                "gate": gate_payload,
                "resolution": resolution,
                "run": initial_row[0] if initial_row is not None else {},
            }
            job_id = _job_id(
                self._tenant.organization_id,
                run_id,
                f"resume:{gate_id}:{version}",
            )
            stmt_insert_job = pg_insert(JobModel).values(
                organization_id=self._tenant.organization_id,
                id=job_id,
                run_id=run_id,
                kind="resume_run",
                status="queued",
                payload=payload,
                available_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
            stmt_outbox = pg_insert(OutboxModel).values(
                organization_id=self._tenant.organization_id,
                topic="run.resume",
                message_key=str(job_id),
                payload={"job_id": str(job_id), "run_id": run_id},
                available_at=timestamp,
                created_at=timestamp,
            )
            stmt_run_phase = (
                update(RunModel)
                .where(
                    RunModel.organization_id == self._tenant.organization_id,
                    RunModel.id == run_id,
                )
                .values(phase="running", updated_at=timestamp)
            )
            await self._database.execute(connection,stmt_insert_job)
            await self._database.execute(connection,stmt_outbox)
            await self._database.execute(connection,stmt_run_phase)
            await self._append_event(
                connection,
                run_id,
                "gate_resolved",
                {
                    "gate_id": str(gate_id),
                    "gate_type": gate_type,
                    "version": version,
                },
                timestamp,
            )
            stmt_job = (
                select(*_JOB_COLUMNS)
                .where(
                    JobModel.organization_id == self._tenant.organization_id,
                    JobModel.id == job_id,
                )
            )
            job_cursor = await self._database.execute(connection,stmt_job)
            job_row = await job_cursor.fetchone()
        assert job_row is not None
        return _job(job_row)

    async def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RunEvent]:
        stmt = (
            select(
                RunEventModel.seq,
                RunEventModel.run_id,
                RunEventModel.event_type,
                RunEventModel.data,
                RunEventModel.created_at,
            )
            .where(
                RunEventModel.organization_id == self._tenant.organization_id,
                RunEventModel.run_id == run_id,
                RunEventModel.seq > after_seq,
            )
            .order_by(RunEventModel.seq)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            rows = await cursor.fetchall()
        return [RunEvent(*row) for row in rows]

    async def list_outbox(self) -> list[OutboxEntry]:
        stmt = (
            select(
                OutboxModel.id,
                OutboxModel.topic,
                OutboxModel.message_key,
                OutboxModel.payload,
                OutboxModel.status,
                OutboxModel.attempt,
                OutboxModel.lease_expires_at,
                OutboxModel.worker_id,
                OutboxModel.error,
            )
            .where(OutboxModel.organization_id == self._tenant.organization_id)
            .order_by(OutboxModel.id)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            rows = await cursor.fetchall()
        return [OutboxEntry(*row) for row in rows]

    async def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[OutboxEntry]:
        timestamp = now or datetime.now(UTC)
        candidates = (
            select(OutboxModel.id)
            .where(
                OutboxModel.organization_id == self._tenant.organization_id,
                or_(
                    and_(
                        OutboxModel.status == "pending",
                        OutboxModel.available_at <= timestamp,
                    ),
                    and_(
                        OutboxModel.status == "publishing",
                        OutboxModel.lease_expires_at <= timestamp,
                    ),
                ),
            )
            .order_by(OutboxModel.available_at, OutboxModel.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
            .cte("candidates")
        )

        stmt = (
            update(OutboxModel)
            .where(
                OutboxModel.organization_id == self._tenant.organization_id,
                OutboxModel.id == candidates.c.id,
            )
            .values(
                status="publishing",
                attempt=OutboxModel.attempt + 1,
                worker_id=worker_id,
                lease_expires_at=timestamp + timedelta(seconds=LEASE_SECONDS),
                error=None,
            )
            .returning(
                OutboxModel.id,
                OutboxModel.topic,
                OutboxModel.message_key,
                OutboxModel.payload,
                OutboxModel.status,
                OutboxModel.attempt,
                OutboxModel.lease_expires_at,
                OutboxModel.worker_id,
                OutboxModel.error,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            rows = await cursor.fetchall()
        return [OutboxEntry(*row) for row in rows]

    async def mark_outbox_published(
        self,
        entry_id: int,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        stmt = (
            update(OutboxModel)
            .where(
                OutboxModel.organization_id == self._tenant.organization_id,
                OutboxModel.id == entry_id,
                OutboxModel.status == "publishing",
                OutboxModel.worker_id == worker_id,
                OutboxModel.lease_expires_at > timestamp,
            )
            .values(
                status="published",
                published_at=timestamp,
                lease_expires_at=None,
                worker_id=None,
            )
            .returning(OutboxModel.id)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection,stmt)
            if await cursor.fetchone() is None:
                raise LeaseLostError(f"lease perdido para outbox {entry_id}")

    async def fail_outbox(
        self,
        entry_id: int,
        *,
        worker_id: str,
        error: str,
        now: datetime | None = None,
    ) -> OutboxEntry:
        timestamp = now or datetime.now(UTC)
        stmt_check = (
            select(
                OutboxModel.id,
                OutboxModel.topic,
                OutboxModel.message_key,
                OutboxModel.payload,
                OutboxModel.status,
                OutboxModel.attempt,
                OutboxModel.lease_expires_at,
                OutboxModel.worker_id,
                OutboxModel.error,
            )
            .where(
                OutboxModel.organization_id == self._tenant.organization_id,
                OutboxModel.id == entry_id,
                OutboxModel.status == "publishing",
                OutboxModel.worker_id == worker_id,
                OutboxModel.lease_expires_at > timestamp,
            )
            .with_for_update()
        )
        async with self._database.connection(self._tenant) as connection:
            current_cursor = await self._database.execute(connection,stmt_check)
            current_row = await current_cursor.fetchone()
            if current_row is None:
                raise LeaseLostError(f"lease perdido para outbox {entry_id}")
            current = OutboxEntry(*current_row)
            will_retry = current.attempt < OUTBOX_MAX_ATTEMPTS
            delay_seconds = min(5 * (2 ** (current.attempt - 1)), 300)

            stmt_update = (
                update(OutboxModel)
                .where(
                    OutboxModel.organization_id == self._tenant.organization_id,
                    OutboxModel.id == entry_id,
                )
                .values(
                    status="pending" if will_retry else "failed",
                    available_at=timestamp + timedelta(seconds=delay_seconds if will_retry else 0),
                    lease_expires_at=None,
                    worker_id=None,
                    error=error[:2000],
                )
                .returning(
                    OutboxModel.id,
                    OutboxModel.topic,
                    OutboxModel.message_key,
                    OutboxModel.payload,
                    OutboxModel.status,
                    OutboxModel.attempt,
                    OutboxModel.lease_expires_at,
                    OutboxModel.worker_id,
                    OutboxModel.error,
                )
            )
            cursor = await self._database.execute(connection,stmt_update)
            row = await cursor.fetchone()
        assert row is not None
        return OutboxEntry(*row)

    async def cancel_pending_test_runs(
        self,
        *,
        reason: str = "pipeline_v2_reset",
        now: datetime | None = None,
    ) -> CancellationSummary:
        """Cancel every currently pending gate and its non-terminal run/jobs."""
        timestamp = now or datetime.now(UTC)
        async with self._database.connection(self._tenant) as connection:
            gate_cursor = await self._database.execute(
                connection,
                update(RunGateModel)
                .where(
                    RunGateModel.organization_id
                    == self._tenant.organization_id,
                    RunGateModel.status == "pending",
                )
                .values(
                    status="cancelled",
                    resolution={"reason": reason},
                    resolved_at=timestamp,
                )
                .returning(RunGateModel.run_id),
            )
            gate_rows = await gate_cursor.fetchall()
            run_ids = sorted({str(row[0]) for row in gate_rows})
            if not run_ids:
                return CancellationSummary(gates=0, runs=0, jobs=0)

            run_cursor = await self._database.execute(
                connection,
                update(RunModel)
                .where(
                    RunModel.organization_id == self._tenant.organization_id,
                    RunModel.id.in_(run_ids),
                    RunModel.phase.in_(
                        ["running", "editing", "awaiting", "review"]
                    ),
                )
                .values(
                    phase="cancelled",
                    error=reason,
                    updated_at=timestamp,
                )
                .returning(RunModel.id),
            )
            run_rows = await run_cursor.fetchall()
            job_cursor = await self._database.execute(
                connection,
                update(JobModel)
                .where(
                    JobModel.organization_id == self._tenant.organization_id,
                    JobModel.run_id.in_(run_ids),
                    JobModel.status.in_(["queued", "running", "retry"]),
                )
                .values(
                    status="cancelled",
                    error=reason,
                    lease_expires_at=None,
                    worker_id=None,
                    updated_at=timestamp,
                )
                .returning(JobModel.id),
            )
            job_rows = await job_cursor.fetchall()
            for run_id in run_ids:
                await self._append_event(
                    connection,
                    run_id,
                    "run_cancelled",
                    {"reason": reason},
                    timestamp,
                )
        return CancellationSummary(
            gates=len(gate_rows),
            runs=len(run_rows),
            jobs=len(job_rows),
        )

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or datetime.now(UTC)
        async with self._database.connection(self._tenant) as connection:
            await self._append_event(
                connection,
                run_id,
                event_type,
                data,
                timestamp,
            )

    async def _append_event(
        self,
        connection: Any,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        timestamp: datetime,
    ) -> None:
        stmt = pg_insert(RunEventModel).values(
            organization_id=self._tenant.organization_id,
            run_id=run_id,
            event_type=event_type,
            data=data,
            created_at=timestamp,
        )
        await self._database.execute(connection,stmt)
