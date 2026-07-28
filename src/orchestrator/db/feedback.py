"""Repositório PostgreSQL de feedback agregado tenant-scoped."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import RunFeedback
from orchestrator.db.tenancy import TenantContext


class PostgresFeedbackRepository:
    """Persiste summaries de run sob transações tenant-scoped."""

    location = "postgresql"
    exists = True

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def save_feedback(self, run_id: str, summary: dict[str, Any]) -> None:
        stmt = pg_insert(RunFeedback).values(
            organization_id=self._tenant.organization_id,
            run_id=run_id,
            summary=summary,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["organization_id", "run_id"],
            set_={
                "position": stmt.excluded.position,
                "summary": summary,
            },
        )
        async with self._database.connection(self._tenant) as connection:
            await self._database.execute(connection, stmt)

    async def load_feedback(self, run_id: str) -> Optional[dict[str, Any]]:
        stmt = (
            select(RunFeedback.summary)
            .where(
                RunFeedback.organization_id == self._tenant.organization_id,
                RunFeedback.run_id == run_id,
            )
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def load_latest_feedback(self) -> Optional[dict[str, Any]]:
        stmt = (
            select(RunFeedback.summary)
            .where(RunFeedback.organization_id == self._tenant.organization_id)
            .order_by(RunFeedback.position.desc(), RunFeedback.run_id.desc())
            .limit(1)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        return row[0] if row is not None else None

