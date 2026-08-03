"""Repositório PostgreSQL de creators tenant-scoped."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.creators import normalize_creator_fields
from orchestrator.db.database import Database
from orchestrator.db.models import Creator
from orchestrator.db.tenancy import TenantContext


def _canonical_voice_design_fields(creator: dict[str, Any]) -> dict[str, Any]:
    """Project graph voice state onto the immutable migration-0010 columns."""
    batch = creator.get("voice_design_batch")
    batch = batch if isinstance(batch, dict) else {}
    candidates = creator.get("voice_candidates") or batch.get("candidates") or []
    canonical_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        preview = candidate.get("preview")
        preview_uri = (
            preview.get("uri")
            if isinstance(preview, dict)
            else getattr(preview, "uri", None)
        )
        if not isinstance(preview_uri, str) or preview_uri.startswith(
            ("data:", "http://", "https://")
        ):
            raise ValueError("voice candidate persistence requires a canonical URI")
        canonical_candidates.append(
            {
                "candidate_id": str(candidate.get("candidate_id") or ""),
                "preview_uri": preview_uri,
                "duration_seconds": float(candidate.get("duration_seconds") or 0.0),
                "media_type": str(candidate.get("media_type") or "audio/mpeg"),
            }
        )

    selected = creator.get("selected_voice_candidate_id") or creator.get(
        "voice_selected_candidate"
    )
    provider = creator.get("voice_provider") or batch.get("provider")
    design_model = creator.get("voice_design_model") or batch.get("design_model")
    design_hash = creator.get("voice_design_hash") or batch.get("description_hash")
    if creator.get("voice_status"):
        voice_status = str(creator["voice_status"])
    elif selected and creator.get("voice_ref"):
        voice_status = "selected"
    elif canonical_candidates:
        voice_status = "candidates_ready"
    else:
        voice_status = "legacy"

    meta: dict[str, Any] = {}
    if batch:
        meta = {
            "prompt_version": str(batch.get("prompt_version") or ""),
            "reroll": int(creator.get("voice_reroll_count") or 0),
            "candidates": canonical_candidates,
            "cost_usd": float(batch.get("cost_usd") or 0.0),
            "cost_source": str(batch.get("cost_source") or "estimate"),
        }
    return {
        "voice_spec": dict(creator.get("voice_spec") or {}),
        "voice_provider": str(provider) if provider else None,
        "voice_design_model": str(design_model) if design_model else None,
        "voice_tts_model": (
            str(creator["voice_tts_model"])
            if creator.get("voice_tts_model")
            else None
        ),
        "voice_design_hash": str(design_hash) if design_hash else None,
        "voice_selected_candidate": str(selected) if selected else None,
        "voice_status": voice_status,
        "voice_design_meta": meta,
    }


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
            columns_cursor = await self._database.execute(
                connection,
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'creators' "
                    "AND column_name = 'voice_spec'"
                    ")"
                ),
            )
            has_voice_design_columns = bool((await columns_cursor.fetchone())[0])
            for creator in creators:
                creator_id = str(creator.get("id") or "")
                fields = normalize_creator_fields(creator)
                voice_fields = _canonical_voice_design_fields(creator)
                status_val = "approved" if creator_id in approved else "rejected"
                insert_values = {
                    "organization_id": self._tenant.organization_id,
                    "run_id": run_id,
                    "creator_id": creator_id,
                    "image_uri": fields["image_uri"],
                    "voice_ref": fields["voice_ref"],
                    "voice_preview_uri": fields["voice_preview_uri"],
                    "angles": fields["angles"],
                    "voice_reroll_count": fields["voice_reroll_count"],
                    "creator_prompt": creator_prompt,
                    "video_prompt": video_prompt,
                    "offer": offer,
                    "status": status_val,
                }
                update_values = {
                    "image_uri": fields["image_uri"],
                    "voice_ref": fields["voice_ref"],
                    "voice_preview_uri": fields["voice_preview_uri"],
                    "angles": fields["angles"],
                    "voice_reroll_count": fields["voice_reroll_count"],
                    "creator_prompt": creator_prompt,
                    "video_prompt": video_prompt,
                    "offer": offer,
                    "status": status_val,
                }
                if has_voice_design_columns:
                    insert_values.update(voice_fields)
                    update_values.update(voice_fields)
                stmt = pg_insert(Creator).values(
                    **insert_values,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["organization_id", "run_id", "creator_id"],
                    set_={
                        "position": stmt.excluded.position,
                        **update_values,
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
                Creator.voice_spec,
                Creator.voice_provider,
                Creator.voice_design_model,
                Creator.voice_tts_model,
                Creator.voice_design_hash,
                Creator.voice_selected_candidate,
                Creator.voice_status,
                Creator.voice_design_meta,
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
                Creator.voice_spec,
                Creator.voice_provider,
                Creator.voice_design_model,
                Creator.voice_tts_model,
                Creator.voice_design_hash,
                Creator.voice_selected_candidate,
                Creator.voice_status,
                Creator.voice_design_meta,
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
            voice_spec,
            voice_provider,
            voice_design_model,
            voice_tts_model,
            voice_design_hash,
            voice_selected_candidate,
            voice_status,
            voice_design_meta,
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
            "voice_spec": voice_spec,
            "voice_provider": voice_provider,
            "voice_design_model": voice_design_model,
            "voice_tts_model": voice_tts_model,
            "voice_design_hash": voice_design_hash,
            "voice_selected_candidate": voice_selected_candidate,
            "voice_status": voice_status,
            "voice_design_meta": voice_design_meta,
            "creator_prompt": creator_prompt,
            "video_prompt": video_prompt,
            "offer": offer,
            "status": status,
        }
