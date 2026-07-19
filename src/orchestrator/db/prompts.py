"""Repositório PostgreSQL de templates e contexto recente de prompts."""
from __future__ import annotations

from typing import Any

from orchestrator.db.database import Database
from orchestrator.db.tenancy import TenantContext
from orchestrator.prompts import validate_template


class PostgresPromptRepository:
    """Mantém o contrato do store JSON sob transações tenant-scoped."""

    location = "postgresql"
    exists = True

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def save_template(
        self,
        *,
        kind: str,
        title: str,
        text: str,
        desc: str = "",
    ) -> dict[str, Any]:
        kind, title, text, desc = validate_template(
            kind=kind,
            title=title,
            text=text,
            desc=desc,
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                INSERT INTO prompt_templates (
                    organization_id, kind, title, description, text
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (self._tenant.organization_id, kind, title, desc, text),
            )
            template_id = (await cursor.fetchone())[0]
        return {
            "id": str(template_id),
            "kind": kind,
            "title": title,
            "desc": desc,
            "text": text,
        }

    async def list_templates(self, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT id, kind, title, description, text
            FROM prompt_templates
            WHERE organization_id = %s
        """
        params: tuple[object, ...] = (self._tenant.organization_id,)
        if kind is not None:
            query += " AND kind = %s"
            params += (kind,)
        query += " ORDER BY id DESC"
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(query, params)
            rows = await cursor.fetchall()
        return [
            {
                "id": str(template_id),
                "kind": row_kind,
                "title": title,
                "desc": description,
                "text": text,
            }
            for template_id, row_kind, title, description, text in rows
        ]

    async def delete_template(self, template_id: str) -> bool:
        try:
            numeric_id = int(template_id)
        except (TypeError, ValueError):
            return False
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                DELETE FROM prompt_templates
                WHERE organization_id = %s AND id = %s
                RETURNING id
                """,
                (self._tenant.organization_id, numeric_id),
            )
            deleted = await cursor.fetchone()
        return deleted is not None

    async def record_last_used(
        self,
        *,
        creator_prompt: str | None = None,
        video_prompt: str | None = None,
    ) -> None:
        updates = {
            kind: value.strip()
            for kind, value in (("creator", creator_prompt), ("video", video_prompt))
            if isinstance(value, str) and value.strip()
        }
        if not updates:
            return
        async with self._database.connection(self._tenant) as connection:
            for kind, text in updates.items():
                await connection.execute(
                    """
                    INSERT INTO prompt_last_used (organization_id, kind, text)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (organization_id, kind) DO UPDATE
                    SET text = EXCLUDED.text, updated_at = CURRENT_TIMESTAMP
                    """,
                    (self._tenant.organization_id, kind, text),
                )

    async def get_last_used(self) -> dict[str, str]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT kind, text
                FROM prompt_last_used
                WHERE organization_id = %s
                ORDER BY kind
                """,
                (self._tenant.organization_id,),
            )
            rows = await cursor.fetchall()
        return dict(rows)
