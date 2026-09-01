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

Com ``DATABASE_URL``, ``open_repository`` seleciona PostgreSQL tenant-scoped. Sem ela,
o arquivo JSON permanece como fallback determinístico para mock/offline.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol

from orchestrator.creators import normalize_creator_fields as _normalize_creator_fields


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


class CreatorRepository(Protocol):
    location: str
    exists: bool

    async def record_creators(
        self,
        run_id: str,
        creators: list[dict[str, Any]],
        *,
        approved_ids: list[str],
        creator_prompt: Optional[str] = None,
        video_prompt: Optional[str] = None,
        offer: Optional[str] = None,
    ) -> None: ...

    async def load_creators(self) -> list[dict[str, Any]]: ...

    async def find_creator(
        self,
        creator_id: str,
        run_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]: ...


class JsonCreatorRepository:
    """Fachada assíncrona do store local preservado para mock/offline."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def location(self) -> str:
        return str(self._path)

    @property
    def exists(self) -> bool:
        return self._path.exists()

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
        record_creators(
            self._path,
            run_id,
            creators,
            approved_ids=approved_ids,
            creator_prompt=creator_prompt,
            video_prompt=video_prompt,
            offer=offer,
        )

    async def load_creators(self) -> list[dict[str, Any]]:
        return load_creators(self._path)

    async def find_creator(
        self,
        creator_id: str,
        run_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        for creator in load_creators(self._path):
            if str(creator.get("creator_id") or "") != creator_id:
                continue
            if run_id is not None and str(creator.get("run_id") or "") != run_id:
                continue
            return creator
        return None


@asynccontextmanager
async def open_repository(path: str | Path) -> AsyncIterator[CreatorRepository]:
    """Seleciona PostgreSQL por ``DATABASE_URL``; sem ela, mantém JSON local."""
    from orchestrator.runtime_mode import open_repository_backend

    def postgres_repository(database, tenant):
        from orchestrator.db import PostgresCreatorRepository

        return PostgresCreatorRepository(database, tenant)

    async with open_repository_backend(
        lambda: JsonCreatorRepository(path),
        postgres_repository,
    ) as repository:
        yield repository
