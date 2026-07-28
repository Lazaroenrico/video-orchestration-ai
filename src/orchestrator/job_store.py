"""Seleção da fila durável sem alterar o modo offline local."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from orchestrator.db.jobs import PostgresJobRepository


@asynccontextmanager
async def open_repository() -> AsyncIterator[Optional[PostgresJobRepository]]:
    if not os.environ.get("DATABASE_URL"):
        yield None
        return

    from orchestrator.db import TenantIdentity, get_shared_database

    database = await get_shared_database()
    tenant = await database.resolve_tenant(TenantIdentity.from_env())
    yield PostgresJobRepository(database, tenant)

