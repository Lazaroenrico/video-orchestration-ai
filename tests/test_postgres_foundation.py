"""Integração real da fundação PostgreSQL multi-tenant (ADR-D36, Fase 2)."""
from __future__ import annotations

import asyncio
import logging

import pytest
from click.testing import CliRunner
from psycopg import sql

from orchestrator.cli import cli
from orchestrator.db import (
    Database,
    TenantAuthorizationError,
    TenantIdentity,
    create_organization,
    grant_membership,
    revoke_membership,
    upgrade_database,
)


def _database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://postgres:postgres@{info.host}:{info.port}/{info.dbname}"


def _runtime_database_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE tenant_app LOGIN PASSWORD 'tenant_app';
        END
        $$
        """
    )
    postgresql.execute("GRANT USAGE ON SCHEMA public TO tenant_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenant_app"
    )
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://tenant_app:tenant_app@{info.host}:{info.port}/{info.dbname}"


def _managed_admin_database_url(postgresql) -> str:
    """Simula o papel administrativo não-superuser fornecido pelo Neon."""
    role_name = "managed_migration_admin"
    role = sql.Identifier(role_name)
    database = sql.Identifier(postgresql.info.dbname)
    postgresql.execute("DROP ROLE IF EXISTS orchestrator_runtime")
    postgresql.execute(
        sql.SQL(
            "DO $$ BEGIN CREATE ROLE {} LOGIN PASSWORD 'managed_migration_admin'; "
            "EXCEPTION WHEN duplicate_object THEN "
            "ALTER ROLE {} LOGIN PASSWORD 'managed_migration_admin'; END $$"
        ).format(role, role)
    )
    postgresql.execute(
        sql.SQL(
            "ALTER ROLE {} LOGIN NOSUPERUSER BYPASSRLS "
            "CREATEDB CREATEROLE REPLICATION"
        ).format(role)
    )
    postgresql.execute(
        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, role)
    )
    postgresql.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(role))
    postgresql.commit()
    info = postgresql.info
    return f"postgresql://{role_name}:{role_name}@{info.host}:{info.port}/{info.dbname}"


async def test_tenant_bootstrap_is_idempotent_after_migrations(postgresql):
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity(
        organization_slug="acme",
        organization_name="Acme",
        user_subject="oidc|alice",
    )

    async with Database(runtime_url) as database:
        first = await database.ensure_tenant(identity)
        second = await database.ensure_tenant(identity)

    assert second == first
    assert first.organization_slug == "acme"
    assert first.user_subject == "oidc|alice"


def test_tenant_identity_loads_the_runtime_environment(monkeypatch):
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "northwind")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Northwind Traders")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "oidc|nancy")

    identity = TenantIdentity.from_env()

    assert identity == TenantIdentity(
        organization_slug="northwind",
        organization_name="Northwind Traders",
        user_subject="oidc|nancy",
    )


def test_tenant_identity_fails_fast_when_runtime_environment_is_incomplete(monkeypatch):
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "northwind")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Northwind Traders")
    monkeypatch.delenv("ORCH_USER_SUBJECT", raising=False)

    with pytest.raises(ValueError, match="ORCH_USER_SUBJECT"):
        TenantIdentity.from_env()


async def test_rls_prevents_cross_organization_reads_and_writes(postgresql):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)
    acme_identity = TenantIdentity("acme", "Acme", "oidc|alice")
    globex_identity = TenantIdentity("globex", "Globex", "oidc|bob")

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(acme_identity)
        globex = await database.ensure_tenant(globex_identity)

        async with database.connection(acme) as connection:
            visible = await connection.execute(
                "SELECT id, slug FROM organizations ORDER BY slug"
            )
            visible_rows = await visible.fetchall()
            attempted_update = await connection.execute(
                "UPDATE organizations SET name = 'compromised' WHERE id = %s",
                (globex.organization_id,),
            )

        async with database.connection(globex) as connection:
            globex_row = await connection.execute(
                "SELECT name FROM organizations WHERE id = %s",
                (globex.organization_id,),
            )

            assert await globex_row.fetchone() == ("Globex",)

    assert visible_rows == [(acme.organization_id, "acme")]
    assert attempted_update.rowcount == 0


async def test_rls_hides_user_identities_from_other_organizations(postgresql):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)

    async with Database(runtime_url) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )
        await database.ensure_tenant(
            TenantIdentity("globex", "Globex", "oidc|bob")
        )

        async with database.connection(acme) as connection:
            result = await connection.execute("SELECT subject FROM users ORDER BY subject")
            visible_subjects = await result.fetchall()

    assert visible_subjects == [("oidc|alice",)]


async def test_pool_does_not_leak_tenant_context_between_transactions(postgresql):
    upgrade_database(_database_url(postgresql))
    runtime_url = _runtime_database_url(postgresql)

    async with Database(runtime_url, min_size=1, max_size=1) as database:
        acme = await database.ensure_tenant(
            TenantIdentity("acme", "Acme", "oidc|alice")
        )

        async with database.connection(acme) as connection:
            scoped = await connection.execute("SELECT slug FROM organizations")
            scoped_rows = await scoped.fetchall()

        async with database.connection() as connection:
            unscoped = await connection.execute("SELECT slug FROM organizations")
            unscoped_rows = await unscoped.fetchall()

    assert scoped_rows == [("acme",)]
    assert unscoped_rows == []


async def test_database_connection_closes_cancelled_connection_without_nested_transaction():
    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False
            self.transaction_calls = 0

        def transaction(self):
            self.transaction_calls += 1
            raise AssertionError("o pool já controla a transação da conexão")

        async def close(self):
            self.closed = True

    class FakePoolConnection:
        def __init__(self, connection: FakeConnection) -> None:
            self.connection = connection
            self.exited = False

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_exc):
            self.exited = True
            return False

    class FakePool:
        def __init__(self) -> None:
            self.connection_obj = FakeConnection()
            self.context = FakePoolConnection(self.connection_obj)

        def connection(self):
            return self.context

    pool = FakePool()
    database = Database.__new__(Database)
    database._pool = pool

    with pytest.raises(asyncio.CancelledError):
        async with database.connection() as connection:
            assert connection is pool.connection_obj
            raise asyncio.CancelledError

    assert pool.connection_obj.closed is True
    assert pool.connection_obj.transaction_calls == 0
    assert pool.context.exited is True


async def test_database_connection_preserves_cancellation_when_close_also_fails():
    class FakeConnection:
        async def close(self):
            raise RuntimeError("already closed")

    class FakePoolConnection:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_exc):
            return False

    class FakePool:
        def connection(self):
            return FakePoolConnection()

    database = Database.__new__(Database)
    database._pool = FakePool()

    with pytest.raises(asyncio.CancelledError):
        async with database.connection():
            raise asyncio.CancelledError


async def test_database_execute_accepts_raw_sql_and_params():
    calls: list[tuple[str, tuple[int]]] = []

    class FakeConnection:
        async def execute(self, query, params):
            calls.append((query, params))
            return "cursor"

    result = await Database.execute(FakeConnection(), "SELECT %s", (1,))

    assert result == "cursor"
    assert calls == [("SELECT %s", (1,))]


async def test_close_shared_database_closes_and_forgets_the_open_pool(monkeypatch):
    from orchestrator.db import database as database_module

    class FakePool:
        closed = False

    class FakeDatabase:
        def __init__(self):
            self._pool = FakePool()
            self.closed = False

        async def close(self):
            self.closed = True
            self._pool.closed = True

    shared = FakeDatabase()
    monkeypatch.setattr(database_module, "_shared_database", shared)

    await database_module.close_shared_database()

    assert shared.closed is True
    assert database_module._shared_database is None


async def test_resolve_tenant_bootstraps_each_local_identity_once(monkeypatch):
    database = Database.__new__(Database)
    database._resolved_tenants = {}
    database._resolved_tenants_lock = asyncio.Lock()
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    calls = 0

    async def ensure_tenant(candidate: TenantIdentity):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return candidate.context()

    database.ensure_tenant = ensure_tenant
    monkeypatch.setenv("ORCH_AUTH_MODE", "disabled")

    tenants = await asyncio.gather(
        *(database.resolve_tenant(identity) for _ in range(8))
    )

    assert calls == 1
    assert tenants == [identity.context()] * 8


async def test_resolve_tenant_reauthorizes_every_access_identity(monkeypatch):
    database = Database.__new__(Database)
    identity = TenantIdentity("acme", "Acme", "oidc|alice")
    calls = 0

    async def authorize_tenant(candidate: TenantIdentity):
        nonlocal calls
        calls += 1
        return candidate.context()

    database.authorize_tenant = authorize_tenant
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")

    first = await database.resolve_tenant(identity)
    second = await database.resolve_tenant(identity)

    assert calls == 2
    assert first == second == identity.context()


def test_cli_migrate_upgrades_postgres_idempotently(postgresql):
    database_url = _database_url(postgresql)
    command = ["migrate", "--database-url", database_url]

    first = CliRunner().invoke(cli, command)
    second = CliRunner().invoke(cli, command)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "PostgreSQL migrado" in first.output


def test_cli_migrate_uses_privileged_url_in_staging(postgresql):
    migration_url = _database_url(postgresql)

    result = CliRunner().invoke(
        cli,
        ["migrate"],
        env={
            "ORCH_ENV": "staging",
            "MIGRATION_DATABASE_URL": migration_url,
            "DATABASE_URL": "postgresql://runtime-invalid@127.0.0.1:1/orchestrator",
        },
    )

    assert result.exit_code == 0, result.output
    assert "PostgreSQL migrado" in result.output


def test_cli_provisions_fixed_runtime_role_with_managed_admin(postgresql):
    password = "runtime-secret-for-test"

    result = CliRunner().invoke(
        cli,
        ["db", "provision-runtime"],
        env={
            "MIGRATION_DATABASE_URL": _managed_admin_database_url(postgresql),
            "ORCHESTRATOR_RUNTIME_PASSWORD": password,
        },
    )

    assert result.exit_code == 0, result.output
    assert password not in result.output
    row = postgresql.execute(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolbypassrls,
               rolcreatedb, rolcreaterole, rolreplication
        FROM pg_roles
        WHERE rolname = 'orchestrator_runtime'
        """
    ).fetchone()
    assert row == ("orchestrator_runtime", True, False, False, False, False, False)


def test_cli_refuses_an_existing_runtime_role_that_can_bypass_rls(postgresql):
    migration_url = _managed_admin_database_url(postgresql)
    postgresql.execute("CREATE ROLE orchestrator_runtime LOGIN BYPASSRLS")
    postgresql.commit()

    try:
        result = CliRunner().invoke(
            cli,
            ["db", "provision-runtime"],
            env={
                "MIGRATION_DATABASE_URL": migration_url,
                "ORCHESTRATOR_RUNTIME_PASSWORD": "must-not-be-applied",
            },
        )

        assert result.exit_code == 1
        assert "SUPERUSER/BYPASSRLS/REPLICATION" in result.output
        assert postgresql.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'orchestrator_runtime'"
        ).fetchone() == (True,)
    finally:
        postgresql.execute("DROP ROLE IF EXISTS orchestrator_runtime")
        postgresql.commit()


async def test_database_builds_from_the_portable_database_url(monkeypatch, postgresql):
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    monkeypatch.setenv("DATABASE_URL", _runtime_database_url(postgresql))

    async with Database.from_env() as database:
        tenant = await database.ensure_tenant(
            TenantIdentity("portable", "Portable", "oidc|portable")
        )

    assert tenant.organization_slug == "portable"


def test_database_from_env_fails_fast_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        Database.from_env()


def test_migrations_accept_an_explicit_psycopg_sqlalchemy_url(postgresql):
    sqlalchemy_url = _database_url(postgresql).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )

    upgrade_database(sqlalchemy_url)


def test_migrations_reject_a_non_postgres_database_url():
    with pytest.raises(ValueError, match="postgresql://"):
        upgrade_database("sqlite:///orchestrator.sqlite")


async def test_database_rejects_a_runtime_role_that_bypasses_rls(postgresql):
    with pytest.raises(ValueError, match="SUPERUSER/BYPASSRLS"):
        async with Database(_database_url(postgresql)):
            pass


async def test_membership_authorization_never_bootstraps_access_identity(postgresql):
    migration_url = _database_url(postgresql)
    upgrade_database(migration_url)
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity("acme", "Acme", "access|alice")
    create_organization(migration_url, slug="acme", name="Acme")

    async with Database(runtime_url) as database:
        with pytest.raises(TenantAuthorizationError, match="membership"):
            await database.authorize_tenant(identity)

        grant_membership(
            migration_url,
            organization_slug="acme",
            user_subject="access|alice",
            role="member",
        )
        assert await database.authorize_tenant(identity) == identity.context()

        revoke_membership(
            migration_url,
            organization_slug="acme",
            user_subject="access|alice",
        )
        with pytest.raises(TenantAuthorizationError, match="membership"):
            await database.authorize_tenant(identity)


async def test_resolve_tenant_requires_membership_in_access_mode(
    monkeypatch,
    postgresql,
):
    migration_url = _database_url(postgresql)
    upgrade_database(migration_url)
    runtime_url = _runtime_database_url(postgresql)
    identity = TenantIdentity("locked", "Locked", "access|mallory")
    create_organization(migration_url, slug="locked", name="Locked")
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")

    async with Database(runtime_url) as database:
        with pytest.raises(TenantAuthorizationError):
            await database.resolve_tenant(identity)

    row = postgresql.execute(
        "SELECT count(*) FROM organization_members"
    ).fetchone()
    assert row == (0,)


def test_cli_admin_manages_organization_memberships(postgresql):
    migration_url = _database_url(postgresql)
    upgrade_database(migration_url)
    command_env = {"MIGRATION_DATABASE_URL": migration_url}
    cli_runner = CliRunner()

    created = cli_runner.invoke(
        cli,
        ["db", "org-create", "--slug", "acme", "--name", "Acme Inc."],
        env=command_env,
    )
    granted = cli_runner.invoke(
        cli,
        [
            "db",
            "membership-grant",
            "--organization-slug",
            "acme",
            "--user-subject",
            "access|alice",
            "--role",
            "admin",
        ],
        env=command_env,
    )
    revoked = cli_runner.invoke(
        cli,
        [
            "db",
            "membership-revoke",
            "--organization-slug",
            "acme",
            "--user-subject",
            "access|alice",
        ],
        env=command_env,
    )

    assert created.exit_code == 0, created.output
    assert granted.exit_code == 0, granted.output
    assert revoked.exit_code == 0, revoked.output
    assert "acme" in created.output
    assert "admin" in granted.output
    assert "revogada" in revoked.output
    assert postgresql.execute(
        "SELECT slug, name FROM organizations"
    ).fetchall() == [("acme", "Acme Inc.")]
    assert postgresql.execute(
        "SELECT subject FROM users"
    ).fetchall() == [("access|alice",)]
    assert postgresql.execute(
        "SELECT count(*) FROM organization_members"
    ).fetchone() == (0,)


def test_membership_admin_rejects_invalid_role_and_unknown_organization(postgresql):
    migration_url = _database_url(postgresql)
    upgrade_database(migration_url)

    with pytest.raises(ValueError, match="papel de membership inválido"):
        grant_membership(
            migration_url,
            organization_slug="missing",
            user_subject="access|alice",
            role="superuser",
        )
    with pytest.raises(ValueError, match="não existe"):
        grant_membership(
            migration_url,
            organization_slug="missing",
            user_subject="access|alice",
            role="member",
        )

    result = CliRunner().invoke(
        cli,
        [
            "db",
            "membership-grant",
            "--organization-slug",
            "missing",
            "--user-subject",
            "access|alice",
            "--role",
            "member",
        ],
        env={"MIGRATION_DATABASE_URL": migration_url},
    )
    assert result.exit_code == 1
    assert "não existe" in result.output


def test_migrations_preserve_application_loggers(caplog, postgresql):
    application_logger = logging.getLogger("orchestrator.test.migrations")

    with caplog.at_level(logging.WARNING):
        upgrade_database(_database_url(postgresql))
        application_logger.warning("application logger remains active")

    assert "application logger remains active" in caplog.text
