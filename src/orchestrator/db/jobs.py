"""Fila de execução, eventos e outbox PostgreSQL tenant-scoped."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.types.json import Jsonb

from orchestrator.db.database import Database
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


_JOB_COLUMNS = """
    id, run_id, kind, status, payload, attempt, max_attempts,
    available_at, lease_expires_at, worker_id, error
"""
_QUALIFIED_JOB_COLUMNS = """
    jobs.id, jobs.run_id, jobs.kind, jobs.status, jobs.payload, jobs.attempt,
    jobs.max_attempts, jobs.available_at, jobs.lease_expires_at,
    jobs.worker_id, jobs.error
"""


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
            await connection.execute(
                """
                INSERT INTO runs (
                    organization_id, id, offer, platform, batch_size, phase
                )
                VALUES (%s, %s, %s, %s, %s, 'running')
                ON CONFLICT (organization_id, id) DO NOTHING
                """,
                (
                    self._tenant.organization_id,
                    run_id,
                    offer,
                    platform,
                    batch_size,
                ),
            )
            inserted = await connection.execute(
                """
                INSERT INTO jobs (
                    organization_id, id, run_id, kind, status,
                    payload, max_attempts, available_at, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, 'execute_run', 'queued',
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (organization_id, id) DO NOTHING
                RETURNING id
                """,
                (
                    self._tenant.organization_id,
                    job_id,
                    run_id,
                    Jsonb(payload),
                    max_attempts,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
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
                await connection.execute(
                    """
                    INSERT INTO outbox (
                        organization_id, topic, message_key, payload,
                        available_at, created_at
                    )
                    VALUES (%s, 'run.queued', %s, %s, %s, %s)
                    """,
                    (
                        self._tenant.organization_id,
                        str(job_id),
                        Jsonb({"job_id": str(job_id), "run_id": run_id}),
                        timestamp,
                        timestamp,
                    ),
                )
            cursor = await connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs "
                "WHERE organization_id = %s AND id = %s",
                (self._tenant.organization_id, job_id),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                WITH candidates AS (
                    SELECT id
                    FROM jobs
                    WHERE organization_id = %s
                      AND (
                        (
                          status IN ('queued', 'retry')
                          AND available_at <= %s
                        )
                        OR (
                          status = 'running'
                          AND lease_expires_at <= %s
                        )
                      )
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE jobs AS jobs
                SET status = 'running',
                    attempt = jobs.attempt + 1,
                    worker_id = %s,
                    lease_expires_at = %s + make_interval(secs => %s),
                    error = NULL,
                    updated_at = %s
                FROM candidates
                WHERE jobs.organization_id = %s
                  AND jobs.id = candidates.id
                RETURNING {_QUALIFIED_JOB_COLUMNS}
                """,
                (
                    self._tenant.organization_id,
                    timestamp,
                    timestamp,
                    limit,
                    worker_id,
                    timestamp,
                    LEASE_SECONDS,
                    timestamp,
                    self._tenant.organization_id,
                ),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                UPDATE jobs
                SET status = 'succeeded', lease_expires_at = NULL,
                    updated_at = %s
                WHERE organization_id = %s AND id = %s
                  AND status = 'running' AND worker_id = %s
                  AND lease_expires_at > %s
                RETURNING run_id
                """,
                (
                    timestamp,
                    self._tenant.organization_id,
                    job_id,
                    worker_id,
                    timestamp,
                ),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                UPDATE jobs
                SET lease_expires_at = %s + make_interval(secs => %s),
                    updated_at = %s
                WHERE organization_id = %s AND id = %s
                  AND status = 'running' AND worker_id = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    timestamp,
                    LEASE_SECONDS,
                    timestamp,
                    self._tenant.organization_id,
                    job_id,
                    worker_id,
                    timestamp,
                ),
            )
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
        now: datetime | None = None,
    ) -> Job:
        timestamp = now or datetime.now(UTC)
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM jobs
                WHERE organization_id = %s AND id = %s
                  AND status = 'running' AND worker_id = %s
                  AND lease_expires_at > %s
                FOR UPDATE
                """,
                (
                    self._tenant.organization_id,
                    job_id,
                    worker_id,
                    timestamp,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LeaseLostError(f"lease perdido para job {job_id}")
            current = _job(row)
            will_retry = current.attempt < current.max_attempts
            delay_seconds = min(5 * (2 ** (current.attempt - 1)), 300)
            updated = await connection.execute(
                f"""
                UPDATE jobs
                SET status = %s,
                    available_at = %s + make_interval(secs => %s),
                    lease_expires_at = NULL,
                    worker_id = NULL,
                    error = %s,
                    updated_at = %s
                WHERE organization_id = %s AND id = %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    "retry" if will_retry else "failed",
                    timestamp,
                    delay_seconds if will_retry else 0,
                    error[:2000],
                    timestamp,
                    self._tenant.organization_id,
                    job_id,
                ),
            )
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
                await connection.execute(
                    """
                    UPDATE runs
                    SET phase = 'error', error = %s, updated_at = %s
                    WHERE organization_id = %s AND id = %s
                    """,
                    (
                        failed.error,
                        timestamp,
                        self._tenant.organization_id,
                        failed.run_id,
                    ),
                )
                await self._append_event(
                    connection,
                    failed.run_id,
                    "error",
                    {"message": failed.error or "job failed"},
                    timestamp,
                )
        return failed

    async def get(self, job_id: UUID) -> Job | None:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs "
                "WHERE organization_id = %s AND id = %s",
                (self._tenant.organization_id, job_id),
            )
            row = await cursor.fetchone()
        return _job(row) if row is not None else None

    async def open_gate(
        self,
        run_id: str,
        *,
        gate_type: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> RunGate:
        timestamp = now or datetime.now(UTC)
        async with self._database.connection(self._tenant) as connection:
            locked_run = await connection.execute(
                """
                SELECT id
                FROM runs
                WHERE organization_id = %s AND id = %s
                FOR UPDATE
                """,
                (self._tenant.organization_id, run_id),
            )
            if await locked_run.fetchone() is None:
                raise ValueError(f"run {run_id!r} inexistente")
            versions = await connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) + 1
                FROM run_gates
                WHERE organization_id = %s AND run_id = %s AND gate_type = %s
                """,
                (self._tenant.organization_id, run_id, gate_type),
            )
            version = int((await versions.fetchone())[0])
            gate_id = _job_id(
                self._tenant.organization_id,
                run_id,
                f"gate:{gate_type}:{version}",
            )
            await connection.execute(
                """
                INSERT INTO run_gates (
                    organization_id, id, run_id, gate_type, version,
                    status, payload, created_at
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s)
                """,
                (
                    self._tenant.organization_id,
                    gate_id,
                    run_id,
                    gate_type,
                    version,
                    Jsonb(payload),
                    timestamp,
                ),
            )
            await connection.execute(
                """
                UPDATE runs
                SET phase = %s, updated_at = %s
                WHERE organization_id = %s AND id = %s
                """,
                (
                    "editing" if gate_type == "edit_concepts" else "awaiting",
                    timestamp,
                    self._tenant.organization_id,
                    run_id,
                ),
            )
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
            public_type = (
                "awaiting_concept_edit"
                if gate_type == "edit_concepts"
                else "awaiting_approval"
            )
            public_data = {
                "run_id": run_id,
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT id, run_id, gate_type, version, status, payload, resolution
                FROM run_gates
                WHERE organization_id = %s AND run_id = %s AND status = 'pending'
                ORDER BY version DESC
                LIMIT 1
                """,
                (self._tenant.organization_id, run_id),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                UPDATE run_gates
                SET status = 'resolved', resolution = %s, resolved_at = %s
                WHERE organization_id = %s AND id = %s
                  AND version = %s AND status = 'pending'
                RETURNING run_id, gate_type, payload
                """,
                (
                    Jsonb(resolution),
                    timestamp,
                    self._tenant.organization_id,
                    gate_id,
                    version,
                ),
            )
            gate_row = await cursor.fetchone()
            if gate_row is None:
                raise StaleGateError(
                    f"gate {gate_id} versão {version} está stale"
                )
            run_id, gate_type, gate_payload = gate_row
            initial_cursor = await connection.execute(
                """
                SELECT payload
                FROM jobs
                WHERE organization_id = %s AND run_id = %s
                  AND kind = 'execute_run'
                ORDER BY created_at
                LIMIT 1
                """,
                (self._tenant.organization_id, run_id),
            )
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
            await connection.execute(
                """
                INSERT INTO jobs (
                    organization_id, id, run_id, kind, status, payload,
                    available_at, created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, 'resume_run', 'queued', %s, %s, %s, %s
                )
                """,
                (
                    self._tenant.organization_id,
                    job_id,
                    run_id,
                    Jsonb(payload),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                """
                INSERT INTO outbox (
                    organization_id, topic, message_key, payload,
                    available_at, created_at
                )
                VALUES (%s, 'run.resume', %s, %s, %s, %s)
                """,
                (
                    self._tenant.organization_id,
                    str(job_id),
                    Jsonb({"job_id": str(job_id), "run_id": run_id}),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.execute(
                """
                UPDATE runs
                SET phase = 'running', updated_at = %s
                WHERE organization_id = %s AND id = %s
                """,
                (timestamp, self._tenant.organization_id, run_id),
            )
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
            job_cursor = await connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs "
                "WHERE organization_id = %s AND id = %s",
                (self._tenant.organization_id, job_id),
            )
            job_row = await job_cursor.fetchone()
        assert job_row is not None
        return _job(job_row)

    async def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
    ) -> list[RunEvent]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT seq, run_id, event_type, data, created_at
                FROM run_events
                WHERE organization_id = %s AND run_id = %s AND seq > %s
                ORDER BY seq
                """,
                (self._tenant.organization_id, run_id, after_seq),
            )
            rows = await cursor.fetchall()
        return [RunEvent(*row) for row in rows]

    async def list_outbox(self) -> list[OutboxEntry]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT id, topic, message_key, payload, status, attempt,
                       lease_expires_at, worker_id, error
                FROM outbox
                WHERE organization_id = %s
                ORDER BY id
                """,
                (self._tenant.organization_id,),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                WITH candidates AS (
                    SELECT id
                    FROM outbox
                    WHERE organization_id = %s
                      AND (
                        (status = 'pending' AND available_at <= %s)
                        OR (
                          status = 'publishing'
                          AND lease_expires_at <= %s
                        )
                      )
                    ORDER BY available_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE outbox AS outbox
                SET status = 'publishing',
                    attempt = outbox.attempt + 1,
                    worker_id = %s,
                    lease_expires_at = %s + make_interval(secs => %s),
                    error = NULL
                FROM candidates
                WHERE outbox.organization_id = %s
                  AND outbox.id = candidates.id
                RETURNING outbox.id, outbox.topic, outbox.message_key,
                          outbox.payload, outbox.status, outbox.attempt,
                          outbox.lease_expires_at, outbox.worker_id, outbox.error
                """,
                (
                    self._tenant.organization_id,
                    timestamp,
                    timestamp,
                    limit,
                    worker_id,
                    timestamp,
                    LEASE_SECONDS,
                    self._tenant.organization_id,
                ),
            )
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
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                UPDATE outbox
                SET status = 'published', published_at = %s,
                    lease_expires_at = NULL, worker_id = NULL
                WHERE organization_id = %s AND id = %s
                  AND status = 'publishing' AND worker_id = %s
                  AND lease_expires_at > %s
                RETURNING id
                """,
                (
                    timestamp,
                    self._tenant.organization_id,
                    entry_id,
                    worker_id,
                    timestamp,
                ),
            )
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
        async with self._database.connection(self._tenant) as connection:
            current_cursor = await connection.execute(
                """
                SELECT id, topic, message_key, payload, status, attempt,
                       lease_expires_at, worker_id, error
                FROM outbox
                WHERE organization_id = %s AND id = %s
                  AND status = 'publishing' AND worker_id = %s
                  AND lease_expires_at > %s
                FOR UPDATE
                """,
                (
                    self._tenant.organization_id,
                    entry_id,
                    worker_id,
                    timestamp,
                ),
            )
            current_row = await current_cursor.fetchone()
            if current_row is None:
                raise LeaseLostError(f"lease perdido para outbox {entry_id}")
            current = OutboxEntry(*current_row)
            will_retry = current.attempt < OUTBOX_MAX_ATTEMPTS
            delay_seconds = min(5 * (2 ** (current.attempt - 1)), 300)
            cursor = await connection.execute(
                """
                UPDATE outbox
                SET status = %s,
                    available_at = %s + make_interval(secs => %s),
                    lease_expires_at = NULL,
                    worker_id = NULL,
                    error = %s
                WHERE organization_id = %s AND id = %s
                RETURNING id, topic, message_key, payload, status, attempt,
                          lease_expires_at, worker_id, error
                """,
                (
                    "pending" if will_retry else "failed",
                    timestamp,
                    delay_seconds if will_retry else 0,
                    error[:2000],
                    self._tenant.organization_id,
                    entry_id,
                ),
            )
            row = await cursor.fetchone()
        assert row is not None
        return OutboxEntry(*row)

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
        await connection.execute(
            """
            INSERT INTO run_events (
                organization_id, run_id, event_type, data, created_at
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                self._tenant.organization_id,
                run_id,
                event_type,
                Jsonb(data),
                timestamp,
            ),
        )
