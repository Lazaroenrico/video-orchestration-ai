"""Read model operacional tenant-scoped para reconstrução e diagnóstico de runs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestrator.db.database import Database
from orchestrator.db.tenancy import TenantContext


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class OperationalThresholds:
    """Limites explícitos e testáveis usados pelo monitor operacional."""

    stream_lag_seconds: int = 120
    provider_quota_ratio: float = 0.8
    anomalous_cost_usd: float = 100.0


class PostgresOperations:
    """Reúne, sem mutar, todas as fontes duráveis relacionadas a um run."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def object_inventory(self, storage: Any) -> dict[str, Any]:
        """Confere os ponteiros do backend ativo sem listar ou mutar o bucket."""
        dual = hasattr(storage, "exists_in")
        async with self._database.connection(self._tenant) as connection:
            if dual:
                cursor = await connection.execute(
                    """
                    SELECT storage_backend, storage_key, size_bytes
                    FROM artifacts
                    WHERE organization_id = %s
                    ORDER BY storage_backend, storage_key
                    """,
                    (self._tenant.organization_id,),
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT storage_backend, storage_key, size_bytes
                    FROM artifacts
                    WHERE organization_id = %s AND storage_backend = %s
                    ORDER BY storage_key
                    """,
                    (self._tenant.organization_id, storage.backend),
                )
            rows = await cursor.fetchall()

        missing: list[str] = []
        verified_count = 0
        by_backend: dict[str, dict[str, int]] = {}
        for backend, key, size_bytes in rows:
            stats = by_backend.setdefault(
                backend,
                {"object_count": 0, "expected_bytes": 0, "verified_count": 0},
            )
            stats["object_count"] += 1
            stats["expected_bytes"] += size_bytes or 0
            exists = (
                await storage.exists_in(backend, key)
                if dual
                else await storage.exists(key)
            )
            if exists:
                verified_count += 1
                stats["verified_count"] += 1
            else:
                missing.append(f"{backend}://{key}" if dual else key)
        result = {
            "backend": "dual" if dual else storage.backend,
            "object_count": len(rows),
            "expected_bytes": sum(size_bytes or 0 for _, _, size_bytes in rows),
            "verified_count": verified_count,
            "missing": missing,
        }
        if dual:
            result["by_backend"] = by_backend
        return result

    async def health_snapshot(
        self,
        *,
        now: datetime,
        thresholds: OperationalThresholds | None = None,
    ) -> dict[str, Any]:
        limits = thresholds or OperationalThresholds()
        async with self._database.connection(self._tenant) as connection:
            job_cursor = await connection.execute(
                """
                SELECT status, count(*)
                FROM jobs
                WHERE organization_id = %s
                GROUP BY status
                """,
                (self._tenant.organization_id,),
            )
            job_counts = dict(await job_cursor.fetchall())

            expired_cursor = await connection.execute(
                """
                SELECT count(*)
                FROM jobs
                WHERE organization_id = %s
                  AND status = 'running'
                  AND lease_expires_at <= %s
                """,
                (self._tenant.organization_id, now),
            )
            expired_job_leases = int((await expired_cursor.fetchone())[0])

            outbox_cursor = await connection.execute(
                """
                SELECT status, count(*)
                FROM outbox
                WHERE organization_id = %s
                GROUP BY status
                """,
                (self._tenant.organization_id,),
            )
            outbox_counts = dict(await outbox_cursor.fetchall())

            quota_cursor = await connection.execute(
                """
                SELECT provider, used_units, limit_units
                FROM provider_quotas
                WHERE organization_id = %s
                ORDER BY provider
                """,
                (self._tenant.organization_id,),
            )
            quota_rows = await quota_cursor.fetchall()

            active_cursor = await connection.execute(
                """
                SELECT runs.id, runs.summary, max(run_events.created_at)
                FROM runs
                LEFT JOIN run_events
                  ON run_events.organization_id = runs.organization_id
                 AND run_events.run_id = runs.id
                WHERE runs.organization_id = %s
                  AND runs.phase IN ('running', 'editing', 'awaiting')
                GROUP BY runs.id, runs.summary
                ORDER BY runs.id
                """,
                (self._tenant.organization_id,),
            )
            active_rows = await active_cursor.fetchall()

            signing_cursor = await connection.execute(
                """
                SELECT count(*)
                FROM run_events
                WHERE organization_id = %s
                  AND event_type = 'storage_signing_error'
                """,
                (self._tenant.organization_id,),
            )
            signing_errors = int((await signing_cursor.fetchone())[0])

        quotas = {
            provider: {
                "used_units": used,
                "limit_units": limit,
                "ratio": used / limit if limit else 1.0,
            }
            for provider, used, limit in quota_rows
        }
        active_runs = [
            {
                "run_id": run_id,
                "stream_lag_seconds": (
                    max(0.0, (now - last_event_at).total_seconds())
                    if last_event_at is not None
                    else None
                ),
                "total_cost_usd": float(summary.get("total_cost_usd") or 0),
            }
            for run_id, summary, last_event_at in active_rows
        ]

        alerts: list[dict[str, Any]] = []
        if expired_job_leases:
            alerts.append(
                {
                    "code": "expired_job_lease",
                    "severity": "critical",
                    "value": expired_job_leases,
                }
            )
        if outbox_counts.get("failed", 0):
            alerts.append(
                {
                    "code": "outbox_dlq",
                    "severity": "critical",
                    "value": outbox_counts["failed"],
                }
            )
        if signing_errors:
            alerts.append(
                {
                    "code": "storage_signing_error",
                    "severity": "warning",
                    "value": signing_errors,
                }
            )
        for run in active_runs:
            lag = run["stream_lag_seconds"]
            if lag is None or lag > limits.stream_lag_seconds:
                alerts.append(
                    {
                        "code": "stream_lag",
                        "severity": "warning",
                        "run_id": run["run_id"],
                        "value": lag,
                        "threshold": limits.stream_lag_seconds,
                    }
                )
            if run["total_cost_usd"] >= limits.anomalous_cost_usd:
                alerts.append(
                    {
                        "code": "anomalous_spend",
                        "severity": "warning",
                        "run_id": run["run_id"],
                        "value": run["total_cost_usd"],
                        "threshold": limits.anomalous_cost_usd,
                    }
                )
        for provider, quota in quotas.items():
            if quota["ratio"] >= limits.provider_quota_ratio:
                alerts.append(
                    {
                        "code": "provider_limit",
                        "severity": "warning",
                        "provider": provider,
                        "value": quota["ratio"],
                        "threshold": limits.provider_quota_ratio,
                    }
                )

        return {
            "generated_at": now.isoformat(),
            "organization_id": str(self._tenant.organization_id),
            "metrics": {
                "jobs": job_counts,
                "expired_job_leases": expired_job_leases,
                "outbox": outbox_counts,
                "storage_signing_errors": signing_errors,
                "provider_quotas": quotas,
                "active_runs": active_runs,
            },
            "alerts": alerts,
        }

    async def inspect_run(self, run_id: str) -> dict[str, Any]:
        async with self._database.connection(self._tenant) as connection:
            run_cursor = await connection.execute(
                """
                SELECT id, phase, offer, platform, batch_size, error, error_type,
                       summary, state
                FROM runs
                WHERE organization_id = %s AND id = %s
                """,
                (self._tenant.organization_id, run_id),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                raise ValueError(f"run {run_id!r} inexistente")

            item_cursor = await connection.execute(
                """
                SELECT payload
                FROM run_items
                WHERE organization_id = %s AND run_id = %s
                ORDER BY position
                """,
                (self._tenant.organization_id, run_id),
            )
            item_rows = await item_cursor.fetchall()

            job_cursor = await connection.execute(
                """
                SELECT id, kind, status, attempt, max_attempts, available_at,
                       lease_expires_at, worker_id, error, error_type,
                       created_at, updated_at
                FROM jobs
                WHERE organization_id = %s AND run_id = %s
                ORDER BY created_at, id
                """,
                (self._tenant.organization_id, run_id),
            )
            job_rows = await job_cursor.fetchall()

            gate_cursor = await connection.execute(
                """
                SELECT id, gate_type, version, status, payload, resolution,
                       created_at, resolved_at
                FROM run_gates
                WHERE organization_id = %s AND run_id = %s
                ORDER BY created_at, version
                """,
                (self._tenant.organization_id, run_id),
            )
            gate_rows = await gate_cursor.fetchall()

            event_cursor = await connection.execute(
                """
                SELECT seq, event_type, data, created_at
                FROM run_events
                WHERE organization_id = %s AND run_id = %s
                ORDER BY seq
                """,
                (self._tenant.organization_id, run_id),
            )
            event_rows = await event_cursor.fetchall()

            artifact_cursor = await connection.execute(
                """
                SELECT id, item_id, creator_id, kind, storage_backend, storage_key,
                       content_type, size_bytes, sha256, retention_class, expires_at
                FROM artifacts
                WHERE organization_id = %s AND run_id = %s
                ORDER BY storage_key
                """,
                (self._tenant.organization_id, run_id),
            )
            artifact_rows = await artifact_cursor.fetchall()

            effect_cursor = await connection.execute(
                """
                SELECT effect_key, provider, units, status, result, error, error_type,
                       provider_operation_id, provider_status,
                       created_at, updated_at
                FROM external_effects
                WHERE organization_id = %s AND run_id = %s
                ORDER BY created_at, effect_key
                """,
                (self._tenant.organization_id, run_id),
            )
            effect_rows = await effect_cursor.fetchall()

        summary = run_row[7]
        provider_units: dict[str, int] = {}
        for effect in effect_rows:
            provider_units[effect[1]] = provider_units.get(effect[1], 0) + effect[2]

        return {
            "organization_id": str(self._tenant.organization_id),
            "run": {
                "run_id": run_row[0],
                "phase": run_row[1],
                "offer": run_row[2],
                "platform": run_row[3],
                "batch_size": run_row[4],
                "error": run_row[5],
                "error_type": run_row[6],
                "summary": summary,
                "state": run_row[8],
            },
            "items": [row[0] for row in item_rows],
            "jobs": [
                {
                    "job_id": str(row[0]),
                    "kind": row[1],
                    "status": row[2],
                    "attempt": row[3],
                    "max_attempts": row[4],
                    "available_at": _iso(row[5]),
                    "lease_expires_at": _iso(row[6]),
                    "worker_id": row[7],
                    "error": row[8],
                    "error_type": row[9],
                    "created_at": _iso(row[10]),
                    "updated_at": _iso(row[11]),
                }
                for row in job_rows
            ],
            "gates": [
                {
                    "gate_id": str(row[0]),
                    "gate_type": row[1],
                    "version": row[2],
                    "status": row[3],
                    "payload": row[4],
                    "resolution": row[5],
                    "created_at": _iso(row[6]),
                    "resolved_at": _iso(row[7]),
                }
                for row in gate_rows
            ],
            "events": [
                {
                    "seq": row[0],
                    "event_type": row[1],
                    "data": row[2],
                    "created_at": _iso(row[3]),
                }
                for row in event_rows
            ],
            "artifacts": [
                {
                    "artifact_id": row[0],
                    "item_id": row[1],
                    "creator_id": row[2],
                    "kind": row[3],
                    "storage_backend": row[4],
                    "storage_key": row[5],
                    "content_type": row[6],
                    "size_bytes": row[7],
                    "sha256": row[8],
                    "retention_class": row[9],
                    "expires_at": _iso(row[10]),
                }
                for row in artifact_rows
            ],
            "effects": [
                {
                    "effect_key": row[0],
                    "provider": row[1],
                    "units": row[2],
                    "status": row[3],
                    "result": row[4],
                    "error": row[5],
                    "error_type": row[6],
                    "provider_operation_id": row[7],
                    "provider_status": row[8],
                    "created_at": _iso(row[9]),
                    "updated_at": _iso(row[10]),
                }
                for row in effect_rows
            ],
            "metrics": {
                "artifact_count": len(artifact_rows),
                "artifact_bytes": sum(row[7] or 0 for row in artifact_rows),
                "event_count": len(event_rows),
                "total_cost_usd": float(summary.get("total_cost_usd") or 0),
                "provider_units": provider_units,
            },
        }
