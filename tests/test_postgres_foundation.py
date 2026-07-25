"""Integração real da fundação PostgreSQL multi-tenant (ADR-D36, Fase 2)."""
from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from orchestrator.cli import cli
from orchestrator.db import Database, TenantIdentity, upgrade_database


def _database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


def _runtime_database_url(postgresql) -> str:
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE tenant_app LOGIN;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
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
    return f"postgresql://tenant_app@{info.host}:{info.port}/{info.dbname}"


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


def test_cli_provisions_fixed_runtime_role_without_echoing_password(postgresql):
    password = "runtime-secret-for-test"

    result = CliRunner().invoke(
        cli,
        ["db", "provision-runtime"],
        env={
            "MIGRATION_DATABASE_URL": _database_url(postgresql),
            "ORCHESTRATOR_RUNTIME_PASSWORD": password,
        },
    )

    assert result.exit_code == 0, result.output
    assert password not in result.output
    row = postgresql.execute(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
        FROM pg_roles
        WHERE rolname = 'orchestrator_runtime'
        """
    ).fetchone()
    assert row == ("orchestrator_runtime", True, False, False, False, False)


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


def test_migrations_preserve_application_loggers(caplog, postgresql):
    application_logger = logging.getLogger("orchestrator.test.migrations")

    with caplog.at_level(logging.WARNING):
        upgrade_database(_database_url(postgresql))
        application_logger.warning("application logger remains active")

    assert "application logger remains active" in caplog.text
