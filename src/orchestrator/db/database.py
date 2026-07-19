"""Pool assíncrono e fronteira transacional multi-tenant."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from orchestrator.db.tenancy import TenantContext, TenantIdentity


class Database:
    """Esconde pool, transação e configuração RLS atrás de uma interface pequena."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
        )

    @classmethod
    def from_env(cls) -> "Database":
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL é obrigatória para o backend PostgreSQL")
        return cls(database_url)

    async def __aenter__(self) -> "Database":
        await self._pool.open(wait=True)
        try:
            async with self._pool.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT rolname, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
                role, is_superuser, bypasses_rls = await cursor.fetchone()
                if is_superuser or bypasses_rls:
                    raise ValueError(
                        f"papel runtime {role!r} não pode ter SUPERUSER/BYPASSRLS"
                    )
        except BaseException:
            await self._pool.close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(
        self,
        tenant: TenantContext | None = None,
    ) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                if tenant is not None:
                    await connection.execute(
                        "SELECT set_config('app.organization_id', %s, true)",
                        (str(tenant.organization_id),),
                    )
                    await connection.execute(
                        "SELECT set_config('app.user_id', %s, true)",
                        (str(tenant.user_id),),
                    )
                yield connection

    async def ensure_tenant(self, identity: TenantIdentity) -> TenantContext:
        """Materializa organização, usuário e membership de modo idempotente."""
        tenant = identity.context()
        async with self.connection(tenant) as connection:
            await connection.execute(
                """
                INSERT INTO organizations (id, slug, name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET slug = EXCLUDED.slug, name = EXCLUDED.name
                """,
                (
                    tenant.organization_id,
                    identity.organization_slug,
                    identity.organization_name,
                ),
            )
            await connection.execute(
                """
                INSERT INTO users (id, subject)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (tenant.user_id, identity.user_subject),
            )
            await connection.execute(
                """
                INSERT INTO organization_members (organization_id, user_id, role)
                VALUES (%s, %s, 'owner')
                ON CONFLICT (organization_id, user_id) DO NOTHING
                """,
                (tenant.organization_id, tenant.user_id),
            )
        return tenant
