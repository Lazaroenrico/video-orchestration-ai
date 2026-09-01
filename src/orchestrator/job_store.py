"""Seleção da fila durável sem alterar o modo offline local."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from orchestrator.db.jobs import PostgresJobRepository
from orchestrator.runtime_mode import open_repository_backend


@asynccontextmanager
async def open_repository() -> AsyncIterator[Optional[PostgresJobRepository]]:
    async with open_repository_backend(lambda: None, PostgresJobRepository) as repository:
        yield repository
