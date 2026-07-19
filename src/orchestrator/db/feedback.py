"""Repositório PostgreSQL de feedback agregado tenant-scoped."""
from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from orchestrator.db.database import Database
from orchestrator.db.tenancy import TenantContext


class PostgresFeedbackRepository:
    """Persiste summaries de run sob transações tenant-scoped."""

    location = "postgresql"
    exists = True

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def save_feedback(self, run_id: str, summary: dict[str, Any]) -> None:
        async with self._database.connection(self._tenant) as connection:
            await connection.execute(
                """
                INSERT INTO run_feedback (organization_id, run_id, summary)
                VALUES (%s, %s, %s)
                ON CONFLICT (organization_id, run_id) DO UPDATE
                SET position = EXCLUDED.position,
                    summary = EXCLUDED.summary,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self._tenant.organization_id, run_id, Jsonb(summary)),
            )

    async def load_feedback(self, run_id: str) -> Optional[dict[str, Any]]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT summary
                FROM run_feedback
                WHERE organization_id = %s AND run_id = %s
                """,
                (self._tenant.organization_id, run_id),
            )
            row = await cursor.fetchone()
        return row[0] if row is not None else None

    async def load_latest_feedback(self) -> Optional[dict[str, Any]]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT summary
                FROM run_feedback
                WHERE organization_id = %s
                ORDER BY position DESC, run_id DESC
                LIMIT 1
                """,
                (self._tenant.organization_id,),
            )
            row = await cursor.fetchone()
        return row[0] if row is not None else None
