"""Persistência de feedback agregado por run (Step 10 → Step 1 no próximo ciclo).

O "store" é um único arquivo JSON chaveado por ``run_id``. Cada entrada contém
o summary do run mais um campo interno ``_idx`` (inteiro incremental), usado para
determinar qual run é o mais recente de forma determinística — sem depender de
timestamps do sistema de arquivos, que podem não ser confiáveis em ambientes de CI
ou quando o arquivo é copiado.

Estratégia de ordenação:
    ``_idx`` é atribuído em ``save_feedback`` como ``max(_idx existentes) + 1``
    (ou 0 se o store estiver vazio). ``load_latest_feedback`` retorna a entrada
    cujo ``_idx`` é o maior. Em caso de empate (impossível pela lógica normal),
    o ``run_id`` lexicograficamente maior é usado como desempate.

Formato no disco (escrita determinística)::

    {
      "run-001": {
        "_idx": 0,
        "approved": 8,
        ...
      },
      "run-002": {
        "_idx": 1,
        ...
      }
    }

Com ``DATABASE_URL``, ``open_repository`` seleciona PostgreSQL tenant-scoped. Sem ela,
o arquivo JSON permanece como fallback determinístico para mock/offline.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Protocol


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


def save_feedback(path: str | Path, run_id: str, summary: dict[str, Any]) -> None:
    """Persiste o summary de um run no store JSON em *path*.

    - Acumula múltiplos runs (não sobrescreve outros run_ids).
    - Cria diretórios intermediários se necessário.
    - Atribui ``_idx`` incremental para definir ordem de chegada.
    - Escrita determinística: ``json.dumps(..., indent=2, sort_keys=True)``.

    Se o mesmo *run_id* for salvo novamente, o ``_idx`` é atualizado para
    refletir que esta é a versão mais recente (útil em cenários de retry).
    """
    path = Path(path)
    store = _read_store(path)

    # Calcula próximo índice (ignora o _idx do próprio run_id caso exista,
    # para que um re-save seja tratado como "mais recente").
    current_max = -1
    for rid, entry in store.items():
        if rid == run_id:
            continue
        idx = entry.get("_idx", -1)
        if isinstance(idx, int) and idx > current_max:
            current_max = idx

    new_idx = current_max + 1

    store[run_id] = {"_idx": new_idx, **summary}
    _write_store(path, store)


def load_feedback(path: str | Path, run_id: str) -> dict[str, Any] | None:
    """Retorna o summary de *run_id*, ou ``None`` se ausente/store inexistente.

    O campo interno ``_idx`` é removido antes de retornar.
    """
    store = _read_store(Path(path))
    entry = store.get(run_id)
    if entry is None:
        return None
    return {k: v for k, v in entry.items() if k != "_idx"}


def load_latest_feedback(path: str | Path) -> dict[str, Any] | None:
    """Retorna o summary do run mais recente, ou ``None`` se o store não existir/estiver vazio.

    "Mais recente" é definido pelo maior ``_idx`` (atribuído em ``save_feedback``).
    Em caso de empate (não deve ocorrer em uso normal), o ``run_id`` lexicograficamente
    maior é usado como critério de desempate.

    O campo interno ``_idx`` é removido antes de retornar.
    """
    store = _read_store(Path(path))
    if not store:
        return None

    # Filtra entradas que possuam _idx válido
    valid = {rid: entry for rid, entry in store.items() if isinstance(entry.get("_idx"), int)}
    if not valid:
        return None

    latest_rid = max(valid, key=lambda rid: (valid[rid]["_idx"], rid))
    return {k: v for k, v in valid[latest_rid].items() if k != "_idx"}


class FeedbackRepository(Protocol):
    location: str
    exists: bool

    async def save_feedback(self, run_id: str, summary: dict[str, Any]) -> None: ...

    async def load_feedback(self, run_id: str) -> dict[str, Any] | None: ...

    async def load_latest_feedback(self) -> dict[str, Any] | None: ...


class JsonFeedbackRepository:
    """Fachada assíncrona do store local preservado para mock/offline."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def location(self) -> str:
        return str(self._path)

    @property
    def exists(self) -> bool:
        return self._path.exists()

    async def save_feedback(self, run_id: str, summary: dict[str, Any]) -> None:
        save_feedback(self._path, run_id, summary)

    async def load_feedback(self, run_id: str) -> dict[str, Any] | None:
        return load_feedback(self._path, run_id)

    async def load_latest_feedback(self) -> dict[str, Any] | None:
        return load_latest_feedback(self._path)


@asynccontextmanager
async def open_repository(path: str | Path) -> AsyncIterator[FeedbackRepository]:
    """Seleciona PostgreSQL por ``DATABASE_URL``; sem ela, mantém JSON local."""
    if not os.environ.get("DATABASE_URL"):
        yield JsonFeedbackRepository(path)
        return

    from orchestrator.db import Database, PostgresFeedbackRepository, TenantIdentity

    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        yield PostgresFeedbackRepository(database, tenant)
