"""Seleção do read model durável de runs sem alterar o modo SQLite local."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from orchestrator.db.runs import PostgresRunRepository


@asynccontextmanager
async def open_repository() -> AsyncIterator[Optional[PostgresRunRepository]]:
    """Entrega PostgreSQL quando configurado; local continua no checkpointer atual."""
    if not os.environ.get("DATABASE_URL"):
        yield None
        return

    from orchestrator.db import Database, TenantIdentity

    async with Database.from_env() as database:
        tenant = await database.resolve_tenant(TenantIdentity.from_env())
        yield PostgresRunRepository(database, tenant)
