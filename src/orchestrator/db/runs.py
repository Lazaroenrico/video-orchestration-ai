"""Read model PostgreSQL tenant-scoped de runs e run_items."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from psycopg.types.json import Jsonb

from orchestrator.db.database import Database
from orchestrator.db.tenancy import TenantContext

_RUN_PHASES = frozenset({"running", "editing", "awaiting", "done", "error"})


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    phase: str
    offer: Optional[str] = None
    platform: Optional[str] = None
    batch_size: Optional[int] = None
    error: Optional[str] = None
    summary: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunIndexEntry:
    run_id: str
    phase: str
    error: Optional[str] = None


class PostgresRunRepository:
    """Persiste o estado público do run sem expor SQL às superfícies HTTP/CLI."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def start(
        self,
        run_id: str,
        *,
        offer: Optional[str] = None,
        platform: Optional[str] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                INSERT INTO runs (
                    organization_id, id, offer, platform, batch_size, phase
                )
                VALUES (%s, %s, %s, %s, %s, 'running')
                ON CONFLICT (organization_id, id) DO UPDATE
                SET offer = EXCLUDED.offer,
                    platform = EXCLUDED.platform,
                    batch_size = EXCLUDED.batch_size,
                    phase = 'running',
                    error = NULL,
                    summary = '{}'::jsonb,
                    state = '{}'::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self._tenant.organization_id,
                    run_id,
                    offer,
                    platform,
                    batch_size,
                ),
            )
            await connection.execute(
                """
                DELETE FROM run_items
                WHERE organization_id = %s AND run_id = %s
                """,
                (self._tenant.organization_id, run_id),
            )

    async def save(
        self,
        run_id: str,
        *,
        phase: str,
        state: dict[str, Any],
        summary: dict[str, Any],
        items: list[dict[str, Any]],
        error: Optional[str] = None,
    ) -> None:
        if phase not in _RUN_PHASES:
            raise ValueError(f"unknown run phase {phase!r}")
        item_ids: list[str] = []
        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                raise ValueError("run item must have a non-empty id")
            item_ids.append(item_id)
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                INSERT INTO runs (
                    organization_id, id, phase, error, summary, state
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (organization_id, id) DO UPDATE
                SET phase = EXCLUDED.phase,
                    error = EXCLUDED.error,
                    summary = EXCLUDED.summary,
                    state = EXCLUDED.state,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self._tenant.organization_id,
                    run_id,
                    phase,
                    error,
                    Jsonb(summary),
                    Jsonb(state),
                ),
            )
            for item, item_id in zip(items, item_ids):
                await connection.execute(
                    """
                    INSERT INTO run_items (
                        organization_id, run_id, item_id, payload
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (organization_id, run_id, item_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self._tenant.organization_id,
                        run_id,
                        item_id,
                        Jsonb(item),
                    ),
                )
            await connection.execute(
                """
                DELETE FROM run_items
                WHERE organization_id = %s
                  AND run_id = %s
                  AND item_id <> ALL(%s::text[])
                """,
                (
                    self._tenant.organization_id,
                    run_id,
                    item_ids,
                ),
            )

    async def get(self, run_id: str) -> Optional[RunSnapshot]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT id, offer, platform, batch_size, phase, error, summary, state
                FROM runs
                WHERE organization_id = %s AND id = %s
                """,
                (self._tenant.organization_id, run_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            items_cursor = await connection.execute(
                """
                SELECT payload
                FROM run_items
                WHERE organization_id = %s AND run_id = %s
                ORDER BY position
                """,
                (self._tenant.organization_id, run_id),
            )
            items = [item_row[0] for item_row in await items_cursor.fetchall()]

        return RunSnapshot(
            run_id=row[0],
            offer=row[1],
            platform=row[2],
            batch_size=row[3],
            phase=row[4],
            error=row[5],
            summary=row[6],
            state=row[7],
            items=items,
        )

    async def list_index(self) -> list[RunIndexEntry]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT id, phase, error
                FROM runs
                WHERE organization_id = %s
                ORDER BY position DESC
                """,
                (self._tenant.organization_id,),
            )
            rows = await cursor.fetchall()
        return [
            RunIndexEntry(run_id=row[0], phase=row[1], error=row[2])
            for row in rows
        ]
