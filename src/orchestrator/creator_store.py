"""Persistência de creators aprovados/rejeitados por run.

O "store" é um único arquivo JSON chaveado por ``f'{run_id}:{creator_id}'``.
Cada entrada contém os campos do creator, prompts, offer, status e um campo
interno ``_idx`` (inteiro incremental global) para ordenação determinística.

Estratégia de ordenação:
    ``_idx`` é atribuído em ``record_creators`` como ``max(_idx existentes) + 1``
    (ou 0 se o store estiver vazio). ``load_creators`` ordena por ``_idx`` desc
    (mais recentes primeiro).

Formato no disco (escrita determinística)::

    {
      "run-001:creator-0": {
        "_idx": 0,
        "run_id": "run-001",
        "creator_id": "creator-0",
        "image": "mock://img/0.png",
        "voice": "voice-0",
        "creator_prompt": null,
        "video_prompt": null,
        "offer": null,
        "status": "approved"
      },
      ...
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _read_store(path: Path) -> dict[str, Any]:
    """Lê o store do disco; retorna dict vazio se o arquivo não existir."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_store(path: Path, data: dict[str, Any]) -> None:
    """Escreve o store de forma determinística (indent=2, sort_keys=True)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _normalize_creator_fields(creator: dict[str, Any]) -> dict[str, Any]:
    """Campos normalizados de creator, preservando aliases legados image/voice."""
    image_uri = (
        creator.get("image_uri")
        or creator.get("image")
        or creator.get("upscaled_base")
    )
    voice_ref = (
        creator.get("voice_ref")
        or creator.get("voice")
        or creator.get("voice_id")
    )
    voice_preview_uri = (
        creator.get("voice_preview_uri")
        or creator.get("voice_preview")
        or creator.get("preview_uri")
    )
    return {
        "image_uri": image_uri,
        "voice_ref": voice_ref,
        "voice_preview_uri": voice_preview_uri,
        "image": image_uri,
        "voice": voice_ref,
        "angles": list(creator.get("angles") or []),
        "voice_reroll_count": creator.get("voice_reroll_count"),
    }


def record_creators(
    path: str | Path,
    run_id: str,
    creators: list[dict[str, Any]],
    *,
    approved_ids: list[str],
    creator_prompt: Optional[str] = None,
    video_prompt: Optional[str] = None,
    offer: Optional[str] = None,
) -> None:
    """Grava uma entrada por creator, chaveada ``f'{run_id}:{creator_id}'``.

    - ``status`` = 'approved' se ``creator_id`` em ``approved_ids``, senão 'rejected'.
    - ``_idx`` incremental global (mais novo = maior índice).
    - Cria diretórios intermediários se necessário.
    - Escrita determinística: ``json.dumps(..., indent=2, sort_keys=True)``.
    """
    path = Path(path)
    store = _read_store(path)

    # Calcula o próximo índice a partir do máximo atual
    current_max = max(
        (entry.get("_idx", -1) for entry in store.values() if isinstance(entry.get("_idx"), int)),
        default=-1,
    )
    approved_set = set(approved_ids)

    for creator in creators:
        creator_id = creator.get("id", "")
        key = f"{run_id}:{creator_id}"
        current_max += 1
        media_fields = _normalize_creator_fields(creator)
        store[key] = {
            "_idx": current_max,
            "run_id": run_id,
            "creator_id": creator_id,
            **media_fields,
            "creator_prompt": creator_prompt,
            "video_prompt": video_prompt,
            "offer": offer,
            "status": "approved" if creator_id in approved_set else "rejected",
        }

    _write_store(path, store)


def load_creators(path: str | Path) -> list[dict[str, Any]]:
    """Lista todas as entradas, mais recente primeiro (por _idx desc), sem _idx."""
    store = _read_store(Path(path))
    if not store:
        return []

    entries = [
        {**{k: v for k, v in entry.items() if k != "_idx"}, **_normalize_creator_fields(entry)}
        for entry in sorted(
            store.values(),
            key=lambda e: e.get("_idx", 0),
            reverse=True,
        )
    ]
    return entries
