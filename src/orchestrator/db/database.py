"""Pool assíncrono e fronteira transacional multi-tenant."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg import AsyncConnection, AsyncCursor
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.dialects import postgresql

from orchestrator.db.tenancy import TenantContext, TenantIdentity


class TenantAuthorizationError(PermissionError):
    """O usuário foi identificado, mas não possui membership no tenant."""


_shared_database: Database | None = None
_shared_database_lock = asyncio.Lock()


async def get_shared_database() -> Database:
    """Retorna uma instância compartilhada do Database com o pool mantido aberto."""
    global _shared_database
    if _shared_database is None or _shared_database._pool.closed:
        async with _shared_database_lock:
            if _shared_database is None or _shared_database._pool.closed:
                db = Database.from_env()
                await db.open()
                _shared_database = db
    return _shared_database


async def close_shared_database() -> None:
    """Encerra com segurança o pool compartilhado do processo."""
    global _shared_database
    async with _shared_database_lock:
        if _shared_database is not None:
            if not _shared_database._pool.closed:
                await _shared_database.close()
            _shared_database = None


class Database:
    """Esconde pool, transação e configuração RLS atrás de uma interface pequena."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            open=False,
        )
        self._resolved_tenants: dict[TenantIdentity, TenantContext] = {}
        self._resolved_tenants_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "Database":
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL é obrigatória para o backend PostgreSQL")
        return cls(database_url)


    async def __aenter__(self) -> "Database":
        await self.open()
        return self

    async def open(self) -> None:
        """Abre o pool e recusa papéis que conseguem furar RLS."""
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

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._pool.close()

    @staticmethod
    async def execute(
        connection: AsyncConnection,
        query_or_stmt: Any,
        params: Any = None,
    ) -> AsyncCursor:
        """Executa uma string SQL bruta ou uma declaração compilada do SQLAlchemy 2.0."""
        if hasattr(query_or_stmt, "compile"):
            compiled = query_or_stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"render_postcompile_vars": True},
            )
            if compiled.post_compile_params:
                state = compiled._process_parameters_for_postcompile(compiled.params)
                sql_str = state.statement
                raw_params = state.parameters
            else:
                sql_str = str(compiled)
                raw_params = compiled.params or {}

            sql_params = {
                k: json.dumps(v) if isinstance(v, (dict, list)) else v
                for k, v in raw_params.items()
            }
            return await connection.execute(sql_str, sql_params)
        return await connection.execute(query_or_stmt, params)

    @asynccontextmanager
    async def connection(
        self,
        tenant: TenantContext | None = None,
    ) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as connection:
            try:
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
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(connection.close())
                except Exception:
                    pass
                raise

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

    async def authorize_tenant(
        self,
        identity: TenantIdentity,
        verified_email: str | None = None,
    ) -> TenantContext:
        """Resolve uma membership existente ou consome convite pendente por e-mail verificado."""
        tenant = identity.context()
        async with self.connection(tenant) as connection:
            # 1. Verifica se já existe membership ativa para o usuário
            cursor = await connection.execute(
                """
                SELECT membership.role
                FROM organization_members AS membership
                JOIN organizations AS organization
                  ON organization.id = membership.organization_id
                JOIN users AS app_user
                  ON app_user.id = membership.user_id
                WHERE membership.organization_id = %s
                  AND membership.user_id = %s
                  AND organization.slug = %s
                  AND app_user.subject = %s
                """,
                (
                    tenant.organization_id,
                    tenant.user_id,
                    identity.organization_slug,
                    identity.user_subject,
                ),
            )
            row = await cursor.fetchone()
            if row is not None:
                return tenant.with_role(str(row[0]))

            # 2. Sem membership: se não houver e-mail verificado, recusa imediatamente
            if not verified_email or not str(verified_email).strip():
                raise TenantAuthorizationError(
                    "membership inexistente para usuário e organização informados"
                )

            # 3. Tenta consumir convite pendente atomicamente
            claim_cursor = await connection.execute(
                """
                SELECT public.claim_organization_invitation(%s, %s, %s, %s, %s)
                """,
                (
                    tenant.organization_id,
                    tenant.user_id,
                    identity.user_subject,
                    str(verified_email).strip(),
                    None,
                ),
            )
            claim_row = await claim_cursor.fetchone()
            if claim_row is None or claim_row[0] is None:
                raise TenantAuthorizationError(
                    "membership e convite inexistentes para usuário e organização informados"
                )
            role = str(claim_row[0])
            return tenant.with_role(role)

    async def resolve_tenant(self, identity: TenantIdentity) -> TenantContext:
        """Bootstrap local; em Access exige provisionamento administrativo prévio."""
        if os.environ.get("ORCH_AUTH_MODE", "disabled") == "cloudflare_access":
            return await self.authorize_tenant(identity)
        cached = self._resolved_tenants.get(identity)
        if cached is not None:
            return cached
        async with self._resolved_tenants_lock:
            cached = self._resolved_tenants.get(identity)
            if cached is None:
                cached = await self.ensure_tenant(identity)
                self._resolved_tenants[identity] = cached
        return cached
