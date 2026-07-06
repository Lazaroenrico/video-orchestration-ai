"""Persistência de prompts do dashboard (templates + último usado por tipo).

Antes, os templates de prompt viviam só no ``localStorage`` do browser — não
sobreviviam a troca de máquina/browser e nunca chegavam ao servidor. Este store
é um único arquivo JSON (``.orchestrator/prompts.json`` por padrão, override via
``ORCH_PROMPTS``) espelhando o padrão de ``creator_store.py``:

    {
      "templates": {
        "1": {"id": "1", "_idx": 1, "kind": "creator", "title": "...",
               "desc": "...", "text": "..."}
      },
      "last_used": {"creator": "...", "video": "..."}
    }

``_idx`` incremental global define "mais recente" de forma determinística
(timestamps de FS não são confiáveis em CI/containers). ``last_used`` guarda o
último prompt enviado num run por tipo — a UI usa como valor inicial das
textareas quando não há rascunho local.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

KINDS = ("creator", "video")


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
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r} (esperado um de {KINDS})")
    title = (title or "").strip()
    text = (text or "").strip()
    if not title:
        raise ValueError("title é obrigatório")
    if not text:
        raise ValueError("text é obrigatório")

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
        "desc": (desc or "").strip(),
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
