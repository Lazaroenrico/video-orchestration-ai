"""Repositório PostgreSQL de templates e contexto recente de prompts."""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database
from orchestrator.db.models import PromptLastUsed, PromptTemplate
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
        stmt = (
            pg_insert(PromptTemplate)
            .values(
                organization_id=self._tenant.organization_id,
                kind=kind,
                title=title,
                description=desc,
                text=text,
            )
            .returning(PromptTemplate.id)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            template_id = (await cursor.fetchone())[0]
        return {
            "id": str(template_id),
            "kind": kind,
            "title": title,
            "desc": desc,
            "text": text,
        }

    async def list_templates(self, kind: str | None = None) -> list[dict[str, Any]]:
        stmt = (
            select(
                PromptTemplate.id,
                PromptTemplate.kind,
                PromptTemplate.title,
                PromptTemplate.description,
                PromptTemplate.text,
            )
            .where(PromptTemplate.organization_id == self._tenant.organization_id)
            .order_by(PromptTemplate.id.desc())
        )
        if kind is not None:
            stmt = stmt.where(PromptTemplate.kind == kind)

        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
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
        stmt = (
            delete(PromptTemplate)
            .where(
                PromptTemplate.organization_id == self._tenant.organization_id,
                PromptTemplate.id == numeric_id,
            )
            .returning(PromptTemplate.id)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
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
            for kind, text_val in updates.items():
                stmt = (
                    pg_insert(PromptLastUsed)
                    .values(
                        organization_id=self._tenant.organization_id,
                        kind=kind,
                        text=text_val,
                    )
                    .on_conflict_do_update(
                        index_elements=["organization_id", "kind"],
                        set_={"text": text_val},
                    )
                )
                await self._database.execute(connection, stmt)

    async def get_last_used(self) -> dict[str, str]:
        stmt = (
            select(PromptLastUsed.kind, PromptLastUsed.text)
            .where(PromptLastUsed.organization_id == self._tenant.organization_id)
            .order_by(PromptLastUsed.kind)
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
        return dict(rows)

