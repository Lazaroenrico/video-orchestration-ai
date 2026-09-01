"""Resolução centralizada do modo de execução e dos knobs de ambiente.

Ponto único de leitura das variáveis que decidem o perfil de execução:

- ``DATABASE_URL`` — a presença define o modo durável (PostgreSQL/R2/fila) versus
  o modo local/offline (SQLite/JSON).
- ``ORCH_ENABLE_PAID_ADAPTERS`` — libera adapters pagos em execução durável.

Toda leitura acontece **na chamada**, nunca no import. Isso preserva a semântica
preguiçosa dos fluxos existentes (que liam ``os.environ`` no ponto de uso) e
permite que testes alternem o ambiente via ``monkeypatch`` entre chamadas.
O módulo não mantém nenhum estado global.

Além dos knobs, concentra o idioma comum das fachadas de store
(``run_store``, ``job_store``, ``creator_store``, ``prompt_store``,
``feedback_store``, ``storage/db``): escolher o repositório PostgreSQL
tenant-scoped quando há infraestrutura durável e o fallback local caso contrário.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Optional, TypeVar

__all__ = [
    "database_url",
    "is_durable",
    "paid_adapters_enabled",
    "open_repository_backend",
]

# Mesmo conjunto aceito pelos parses originais (sem strip, apenas lower-case).
_TRUTHY = frozenset({"1", "true", "yes"})

R = TypeVar("R")


def database_url() -> Optional[str]:
    """Lê ``DATABASE_URL`` do ambiente em cada chamada; ``None`` quando ausente."""
    return os.environ.get("DATABASE_URL")


def is_durable() -> bool:
    """``True`` quando há infraestrutura durável configurada (``DATABASE_URL``)."""
    return bool(database_url())


def paid_adapters_enabled() -> bool:
    """Lê ``ORCH_ENABLE_PAID_ADAPTERS`` tardiamente.

    Aceita exatamente ``1``, ``true`` ou ``yes`` (case-insensitive), mesma regra
    dos parses que este módulo substitui; qualquer outro valor ou ausência é falso.
    """
    return os.environ.get("ORCH_ENABLE_PAID_ADAPTERS", "").lower() in _TRUTHY


@asynccontextmanager
async def open_repository_backend(
    local_factory: Callable[[], R],
    postgres_factory: Callable[..., R],
) -> AsyncIterator[R]:
    """Seleciona o backend de um store por ``DATABASE_URL``.

    Sem modo durável, entrega ``local_factory()`` (JSON/SQLite local). Com modo
    durável, resolve o banco compartilhado e o tenant do ambiente e entrega
    ``postgres_factory(database, tenant)``. Os imports da stack PostgreSQL ficam
    dentro desta função para preservar o carregamento tardio no modo mock/local.
    """
    if not is_durable():
        yield local_factory()
        return

    from orchestrator.db import TenantIdentity, get_shared_database

    database = await get_shared_database()
    tenant = await database.resolve_tenant(TenantIdentity.from_env())
    yield postgres_factory(database, tenant)
