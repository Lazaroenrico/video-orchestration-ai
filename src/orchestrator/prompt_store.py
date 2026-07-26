"""Persistência de prompts do dashboard (templates + último usado por tipo).

Antes, os templates de prompt viviam só no ``localStorage`` do browser. O primeiro
backend no servidor foi um arquivo JSON (``.orchestrator/prompts.json`` por padrão,
override via ``ORCH_PROMPTS``), ainda preservado para mock/offline:

    {
      "templates": {
        "1": {"id": "1", "_idx": 1, "kind": "creator", "title": "...",
               "desc": "...", "text": "..."}
      },
      "last_used": {"creator": "...", "video": "..."}
    }

Com ``DATABASE_URL``, ``open_repository`` seleciona o repositório PostgreSQL
tenant-scoped da ADR-D36. Sem ela, ``_idx`` incremental global no JSON define
"mais recente" de forma determinística
(timestamps de FS não são confiáveis em CI/containers). ``last_used`` guarda o
último prompt enviado num run por tipo — a UI usa como valor inicial das
textareas quando não há rascunho local. Os dois backends mantêm o mesmo contrato.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol

from orchestrator.prompts import KINDS, validate_template


def _read_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def save_template(
    path: str | Path,
    *,
    kind: str,
    title: str,
    text: str,
    desc: str = "",
) -> dict[str, Any]:
    """Grava um template; retorna a entrada salva (com ``id``)."""
    kind, title, text, desc = validate_template(
        kind=kind,
        title=title,
        text=text,
        desc=desc,
    )

    path = Path(path)
    store = _read_store(path)
    templates = store.setdefault("templates", {})

    current_max = max(
        (t.get("_idx", -1) for t in templates.values() if isinstance(t, dict)),
        default=-1,
    )
    idx = current_max + 1
    entry = {
        "id": str(idx),
        "_idx": idx,
        "kind": kind,
        "title": title,
        "desc": desc,
        "text": text,
    }
    templates[entry["id"]] = entry
    _write_store(path, store)
    return {k: v for k, v in entry.items() if k != "_idx"}


def list_templates(path: str | Path, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """Templates mais recentes primeiro (por ``_idx`` desc), sem o campo interno."""
    templates = _read_store(Path(path)).get("templates", {})
    entries = sorted(
        (t for t in templates.values() if isinstance(t, dict)),
        key=lambda t: t.get("_idx", 0),
        reverse=True,
    )
    if kind is not None:
        entries = [t for t in entries if t.get("kind") == kind]
    return [{k: v for k, v in t.items() if k != "_idx"} for t in entries]


def delete_template(path: str | Path, template_id: str) -> bool:
    """Remove um template pelo id; ``False`` se ele não existe."""
    path = Path(path)
    store = _read_store(path)
    templates = store.get("templates", {})
    if str(template_id) not in templates:
        return False
    del templates[str(template_id)]
    _write_store(path, store)
    return True


def record_last_used(
    path: str | Path,
    *,
    creator_prompt: Optional[str] = None,
    video_prompt: Optional[str] = None,
) -> None:
    """Registra o último prompt usado por tipo; vazio/None preserva o anterior."""
    updates = {
        kind: value.strip()
        for kind, value in (("creator", creator_prompt), ("video", video_prompt))
        if isinstance(value, str) and value.strip()
    }
    if not updates:
        return
    path = Path(path)
    store = _read_store(path)
    store.setdefault("last_used", {}).update(updates)
    _write_store(path, store)


def get_last_used(path: str | Path) -> dict[str, str]:
    last = _read_store(Path(path)).get("last_used", {})
    return {k: v for k, v in last.items() if k in KINDS and isinstance(v, str) and v}


class PromptRepository(Protocol):
    location: str
    exists: bool

    async def save_template(self, **values: Any) -> dict[str, Any]: ...

    async def list_templates(self, kind: Optional[str] = None) -> list[dict[str, Any]]: ...

    async def delete_template(self, template_id: str) -> bool: ...

    async def record_last_used(
        self,
        *,
        creator_prompt: Optional[str] = None,
        video_prompt: Optional[str] = None,
    ) -> None: ...

    async def get_last_used(self) -> dict[str, str]: ...


class JsonPromptRepository:
    """Fachada assíncrona do store local, preservado para mock/offline."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def location(self) -> str:
        return str(self._path)

    @property
    def exists(self) -> bool:
        return self._path.exists()

    async def save_template(self, **values: Any) -> dict[str, Any]:
        return save_template(self._path, **values)

    async def list_templates(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        return list_templates(self._path, kind)

    async def delete_template(self, template_id: str) -> bool:
        return delete_template(self._path, template_id)

    async def record_last_used(
        self,
        *,
        creator_prompt: Optional[str] = None,
        video_prompt: Optional[str] = None,
    ) -> None:
        record_last_used(
            self._path,
            creator_prompt=creator_prompt,
            video_prompt=video_prompt,
        )

    async def get_last_used(self) -> dict[str, str]:
        return get_last_used(self._path)


@asynccontextmanager
async def open_repository(path: str | Path) -> AsyncIterator[PromptRepository]:
    """Seleciona PostgreSQL por ``DATABASE_URL``; sem ela, mantém JSON local."""
    if not os.environ.get("DATABASE_URL"):
        yield JsonPromptRepository(path)
        return

    # Imports tardios evitam carregar a stack PostgreSQL no modo mock/local.
    from orchestrator.db import Database, PostgresPromptRepository, TenantIdentity

    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        yield PostgresPromptRepository(database, tenant)
