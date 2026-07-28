"""Repositório PostgreSQL de creators tenant-scoped."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.creators import normalize_creator_fields
from orchestrator.db.database import Database
from orchestrator.db.models import Creator
from orchestrator.db.tenancy import TenantContext


class PostgresCreatorRepository:
    """Mantém o contrato observável do store JSON de creators."""

    location = "postgresql"
    exists = True

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def record_creators(
        self,
        run_id: str,
        creators: list[dict[str, Any]],
        *,
        approved_ids: list[str],
        creator_prompt: Optional[str] = None,
        video_prompt: Optional[str] = None,
        offer: Optional[str] = None,
    ) -> None:
        approved = set(approved_ids)
        async with self._database.connection(self._tenant) as connection:
            for creator in creators:
                creator_id = str(creator.get("id") or "")
                fields = normalize_creator_fields(creator)
                status_val = "approved" if creator_id in approved else "rejected"
                stmt = pg_insert(Creator).values(
                    organization_id=self._tenant.organization_id,
                    run_id=run_id,
                    creator_id=creator_id,
                    image_uri=fields["image_uri"],
                    voice_ref=fields["voice_ref"],
                    voice_preview_uri=fields["voice_preview_uri"],
                    angles=fields["angles"],
                    voice_reroll_count=fields["voice_reroll_count"],
                    creator_prompt=creator_prompt,
                    video_prompt=video_prompt,
                    offer=offer,
                    status=status_val,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["organization_id", "run_id", "creator_id"],
                    set_={
                        "position": stmt.excluded.position,
                        "image_uri": fields["image_uri"],
                        "voice_ref": fields["voice_ref"],
                        "voice_preview_uri": fields["voice_preview_uri"],
                        "angles": fields["angles"],
                        "voice_reroll_count": fields["voice_reroll_count"],
                        "creator_prompt": creator_prompt,
                        "video_prompt": video_prompt,
                        "offer": offer,
                        "status": status_val,
                    },
                )
                await self._database.execute(connection, stmt)

    async def load_creators(self) -> list[dict[str, Any]]:
        stmt = (
            select(
                Creator.run_id,
                Creator.creator_id,
                Creator.image_uri,
                Creator.voice_ref,
                Creator.voice_preview_uri,
                Creator.angles,
                Creator.voice_reroll_count,
                Creator.creator_prompt,
                Creator.video_prompt,
                Creator.offer,
                Creator.status,
            )
            .where(Creator.organization_id == self._tenant.organization_id)
            .order_by(Creator.position.desc())
        )
        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
        return [self._creator_from_row(row) for row in rows]

    async def find_creator(
        self,
        creator_id: str,
        run_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        stmt = (
            select(
                Creator.run_id,
                Creator.creator_id,
                Creator.image_uri,
                Creator.voice_ref,
                Creator.voice_preview_uri,
                Creator.angles,
                Creator.voice_reroll_count,
                Creator.creator_prompt,
                Creator.video_prompt,
                Creator.offer,
                Creator.status,
            )
            .where(
                Creator.organization_id == self._tenant.organization_id,
                Creator.creator_id == creator_id,
            )
            .order_by(Creator.position.desc())
            .limit(1)
        )
        if run_id is not None:
            stmt = stmt.where(Creator.run_id == run_id)

        async with self._database.connection(self._tenant) as connection:
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
        return self._creator_from_row(row) if row is not None else None

    @staticmethod
    def _creator_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        (
            run_id,
            creator_id,
            image_uri,
            voice_ref,
            voice_preview_uri,
            angles,
            voice_reroll_count,
            creator_prompt,
            video_prompt,
            offer,
            status,
        ) = row
        fields = normalize_creator_fields(
            {
                "image_uri": image_uri,
                "voice_ref": voice_ref,
                "voice_preview_uri": voice_preview_uri,
                "angles": angles,
                "voice_reroll_count": voice_reroll_count,
            }
        )
        return {
            "run_id": run_id,
            "creator_id": creator_id,
            **fields,
            "creator_prompt": creator_prompt,
            "video_prompt": video_prompt,
            "offer": offer,
            "status": status,
        }

