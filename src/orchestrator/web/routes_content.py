"""Rotas de conteúdo do dashboard: prompts, histórico de creators e integrações."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import orchestrator.creator_store as creator_store
import orchestrator.prompt_store as prompt_store
from orchestrator.config import default_prompt_store_path
from orchestrator.web.events import (
    _creator_id,
    _has_complete_media,
    _normalize_creator,
    _normalize_creator_history,
)
from orchestrator.web.run_executor import _server_attr
from orchestrator.web.settings import effective_config_dir

_log = logging.getLogger(__name__)

router = APIRouter()


class PromptTemplateRequest(BaseModel):
    kind: str
    title: str
    text: str
    desc: str = ""


@router.get("/api/prompts")
async def prompts_index() -> dict[str, Any]:
    """Templates salvos + último prompt usado, em PostgreSQL ou JSON local."""
    store_path = default_prompt_store_path()
    async with prompt_store.open_repository(store_path) as prompts:
        return {
            "templates": await prompts.list_templates(),
            "last_used": await prompts.get_last_used(),
            "store_path": prompts.location,
            "exists": prompts.exists,
        }


@router.post("/api/prompts")
async def save_prompt_template(req: PromptTemplateRequest) -> dict[str, Any]:
    try:
        async with prompt_store.open_repository(default_prompt_store_path()) as prompts:
            saved = await prompts.save_template(
                kind=req.kind,
                title=req.title,
                text=req.text,
                desc=req.desc,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True, "template": saved}


@router.delete("/api/prompts/{template_id}")
async def delete_prompt_template(template_id: str) -> dict[str, Any]:
    async with prompt_store.open_repository(default_prompt_store_path()) as prompts:
        if not await prompts.delete_template(template_id):
            raise HTTPException(status_code=404, detail=f"template {template_id!r} not found")
    return {"ok": True}


@router.get("/api/creators")
async def creators_history() -> dict[str, Any]:
    store_path = _server_attr("default_creator_store_path")()
    async with creator_store.open_repository(store_path) as repository:
        creators = [
            _normalize_creator_history(c)
            for c in await repository.load_creators()
            if _has_complete_media(c)
        ]
        location = repository.location
        exists = repository.exists
    if not creators:
        creators = [
            _normalize_creator_history(c)
            for c in _recover_creators_from_media(_server_attr("default_media_path")())
        ]
    return await _server_attr("_sign_payload")(
        {
            "creators": creators,
            "store_path": location,
            "exists": exists,
        },
        None,
    )


@router.get("/api/integrations")
async def integrations_index(config_dir: Optional[str] = None) -> dict[str, Any]:
    """Mapa stage → adapter lido de providers.yaml (fonte da tela Integrations Hub)."""
    effective = effective_config_dir(config_dir)
    providers = _server_attr("load_providers")(effective)
    agent_catalog = _server_attr("load_agent_catalog")(effective)
    adapters = (providers or {}).get("adapters", {}) or {}
    stages = {str(k): str(v) for k, v in adapters.items()}
    return {"stages": stages, "agents": agent_catalog.as_dict()}


def _pick_first_existing(directory: Path, names: tuple[str, ...]) -> Optional[Path]:
    for name in names:
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _recover_creators_from_media(media_root: Path) -> list[dict[str, Any]]:
    """Reconstrói histórico básico quando o JSON de creators foi zerado."""
    if not media_root.exists():
        return []

    recovered: list[dict[str, Any]] = []
    image_names = ("image.png", "image.svg", "image.jpg", "image.jpeg", "image.webp")
    voice_names = ("voice.wav", "voice.mp3", "voice.m4a", "voice.ogg")

    for run_dir in sorted((p for p in media_root.iterdir() if p.is_dir()), reverse=True):
        creator_dirs = (
            p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("creator-")
        )
        for creator_dir in sorted(creator_dirs):
            image_path = _pick_first_existing(creator_dir, image_names)
            voice_path = _pick_first_existing(creator_dir, voice_names)
            # Só pessoas completas entram na galeria: imagem E voz em disco.
            if image_path is None or voice_path is None:
                continue
            image_uri = f"/media/{run_dir.name}/{creator_dir.name}/{image_path.name}"
            voice_uri = f"/media/{run_dir.name}/{creator_dir.name}/{voice_path.name}"
            recovered.append(
                {
                    "run_id": run_dir.name,
                    "creator_id": creator_dir.name,
                    "id": creator_dir.name,
                    "image_uri": image_uri,
                    "image": image_uri,
                    "voice_ref": voice_uri,
                    "voice": voice_uri,
                    "voice_preview_uri": voice_uri,
                    "angles": [],
                    "creator_prompt": None,
                    "video_prompt": None,
                    "offer": None,
                    "status": "recovered",
                }
            )

    return recovered


async def _find_creator_for_draft_repository(
    creator_id: str,
    creator_run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve no backend ativo e só cai para varredura local quando necessário."""
    async with creator_store.open_repository(_server_attr("default_creator_store_path")()) as repo:
        creator = await repo.find_creator(creator_id, creator_run_id)
    if creator is not None:
        return _normalize_creator(creator)

    for recovered in _recover_creators_from_media(_server_attr("default_media_path")()):
        if _creator_id(recovered) != creator_id:
            continue
        if creator_run_id is not None and str(recovered.get("run_id") or "") != creator_run_id:
            continue
        return _normalize_creator(recovered)

    detail = f"creator {creator_id!r} not found"
    if creator_run_id is not None:
        detail = f"creator {creator_id!r} not found for run {creator_run_id!r}"
    raise HTTPException(status_code=404, detail=detail)
