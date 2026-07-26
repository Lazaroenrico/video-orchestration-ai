"""Repositório PostgreSQL de creators tenant-scoped."""
from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from orchestrator.creators import normalize_creator_fields
from orchestrator.db.database import Database
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
                await connection.execute(
                    """
                    INSERT INTO creators (
                        organization_id, run_id, creator_id, image_uri, voice_ref,
                        voice_preview_uri, angles, voice_reroll_count, creator_prompt,
                        video_prompt, offer, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, run_id, creator_id) DO UPDATE
                    SET position = EXCLUDED.position,
                        image_uri = EXCLUDED.image_uri,
                        voice_ref = EXCLUDED.voice_ref,
                        voice_preview_uri = EXCLUDED.voice_preview_uri,
                        angles = EXCLUDED.angles,
                        voice_reroll_count = EXCLUDED.voice_reroll_count,
                        creator_prompt = EXCLUDED.creator_prompt,
                        video_prompt = EXCLUDED.video_prompt,
                        offer = EXCLUDED.offer,
                        status = EXCLUDED.status,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self._tenant.organization_id,
                        run_id,
                        creator_id,
                        fields["image_uri"],
                        fields["voice_ref"],
                        fields["voice_preview_uri"],
                        Jsonb(fields["angles"]),
                        fields["voice_reroll_count"],
                        creator_prompt,
                        video_prompt,
                        offer,
                        "approved" if creator_id in approved else "rejected",
                    ),
                )

    async def load_creators(self) -> list[dict[str, Any]]:
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT run_id, creator_id, image_uri, voice_ref, voice_preview_uri,
                       angles, voice_reroll_count, creator_prompt, video_prompt,
                       offer, status
                FROM creators
                WHERE organization_id = %s
                ORDER BY position DESC
                """,
                (self._tenant.organization_id,),
            )
            rows = await cursor.fetchall()
        return [self._creator_from_row(row) for row in rows]

    async def find_creator(
        self,
        creator_id: str,
        run_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        query = """
            SELECT run_id, creator_id, image_uri, voice_ref, voice_preview_uri,
                   angles, voice_reroll_count, creator_prompt, video_prompt,
                   offer, status
            FROM creators
            WHERE organization_id = %s AND creator_id = %s
        """
        params: tuple[object, ...] = (self._tenant.organization_id, creator_id)
        if run_id is not None:
            query += " AND run_id = %s"
            params += (run_id,)
        query += " ORDER BY position DESC LIMIT 1"
        async with self._database.connection(self._tenant) as connection:
            cursor = await connection.execute(query, params)
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
