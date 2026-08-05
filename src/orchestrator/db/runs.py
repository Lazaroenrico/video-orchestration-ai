"""Read model PostgreSQL tenant-scoped de runs e run_items."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import Run, RunItem
from orchestrator.db.tenancy import TenantContext

_RUN_PHASES = frozenset(
    {"running", "editing", "awaiting", "review", "done", "error", "cancelled"}
)


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    phase: str
    offer: Optional[str] = None
    platform: Optional[str] = None
    batch_size: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    summary: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunIndexEntry:
    run_id: str
    phase: str
    error: Optional[str] = None
    error_type: Optional[str] = None


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
        stmt_run = (
            pg_insert(Run)
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
            .on_conflict_do_update(
                index_elements=["organization_id", "id"],
                set_={
                    "offer": offer,
                    "platform": platform,
                    "batch_size": batch_size,
                    "phase": "running",
                    "error": None,
                    "error_type": None,
                    "summary": {},
                    "state": {},
                },
            )
        )
        stmt_delete_items = delete(RunItem).where(
            RunItem.organization_id == self._tenant.organization_id,
            RunItem.run_id == run_id,
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt_run)
            await self._database.execute(connection, stmt_delete_items)

    async def save(
        self,
        run_id: str,
        *,
        phase: str,
        state: dict[str, Any],
        summary: dict[str, Any],
        items: list[dict[str, Any]],
        error: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        if phase not in _RUN_PHASES:
            raise ValueError(f"unknown run phase {phase!r}")
        item_ids: list[str] = []
        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                raise ValueError("run item must have a non-empty id")
            item_ids.append(item_id)

        stmt_run = (
            pg_insert(Run)
            .values(
                organization_id=self._tenant.organization_id,
                id=run_id,
                phase=phase,
                error=error,
                error_type=error_type,
                summary=summary,
                state=state,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "id"],
                set_={
                    "phase": phase,
                    "error": error,
                    "error_type": error_type,
                    "summary": summary,
                    "state": state,
                },
            )
        )

        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt_run)
            for item, item_id in zip(items, item_ids):
                stmt_item = (
                    pg_insert(RunItem)
                    .values(
                        organization_id=self._tenant.organization_id,
                        run_id=run_id,
                        item_id=item_id,
                        payload=item,
                    )
                    .on_conflict_do_update(
                        index_elements=["organization_id", "run_id", "item_id"],
                        set_={
                            "payload": item,
                        },
                    )
                )
                await self._database.execute(connection, stmt_item)

            if item_ids:
                stmt_delete = delete(RunItem).where(
                    RunItem.organization_id == self._tenant.organization_id,
                    RunItem.run_id == run_id,
                    RunItem.item_id.notin_(item_ids),
                )
            else:
                stmt_delete = delete(RunItem).where(
                    RunItem.organization_id == self._tenant.organization_id,
                    RunItem.run_id == run_id,
                )
            await self._database.execute(connection, stmt_delete)

    async def get(self, run_id: str) -> Optional[RunSnapshot]:
        stmt_run = (
            select(
                Run.id,
                Run.offer,
                Run.platform,
                Run.batch_size,
                Run.phase,
                Run.error,
                Run.error_type,
                Run.summary,
                Run.state,
            )
            .where(
                Run.organization_id == self._tenant.organization_id,
                Run.id == run_id,
            )
        )
        stmt_items = (
            select(RunItem.payload)
            .where(
                RunItem.organization_id == self._tenant.organization_id,
                RunItem.run_id == run_id,
            )
            .order_by(RunItem.position)
        )

        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt_run)
            row = await cursor.fetchone()
            if row is None:
                return None
            items_cursor = await self._database.execute(connection, stmt_items)
            items = [item_row[0] for item_row in await items_cursor.fetchall()]

        return RunSnapshot(
            run_id=row[0],
            offer=row[1],
            platform=row[2],
            batch_size=row[3],
            phase=row[4],
            error=row[5],
            error_type=row[6],
            summary=row[7],
            state=row[8],
            items=items,
        )

    async def list_index(self) -> list[RunIndexEntry]:
        stmt = (
            select(Run.id, Run.phase, Run.error, Run.error_type)
            .where(Run.organization_id == self._tenant.organization_id)
            .order_by(Run.position.desc())
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
        return [
            RunIndexEntry(run_id=row[0], phase=row[1], error=row[2], error_type=row[3])
            for row in rows
        ]
