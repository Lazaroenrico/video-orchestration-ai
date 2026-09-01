"""Seleção do read model durável de runs sem alterar o modo SQLite local."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from orchestrator.db.runs import PostgresRunRepository
from orchestrator.runtime_mode import open_repository_backend


@asynccontextmanager
async def open_repository() -> AsyncIterator[Optional[PostgresRunRepository]]:
    """Entrega PostgreSQL quando configurado; local continua no checkpointer atual."""
    async with open_repository_backend(lambda: None, PostgresRunRepository) as repository:
        yield repository
