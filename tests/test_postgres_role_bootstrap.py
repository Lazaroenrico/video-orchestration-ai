"""Testes de separação de papéis PostgreSQL, bootstrap idempotente e migração com BYPASSRLS."""
from __future__ import annotations

import pathlib

import psycopg
import pytest

from orchestrator.auth import RequestPrincipal
from orchestrator.db import (
    Database,
    TenantIdentity,
    create_organization,
    grant_membership,
    provision_runtime_role,
    upgrade_database,
)
from orchestrator.db.members import PostgresMemberRepository

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
_APP_ROLE_SQL_PATH = _PROJECT_ROOT / "infra" / "postgres" / "10-app-role.sql"


def _database_url(postgresql) -> str:
    info = postgresql.info
    password = info.password or "postgres"
    return f"postgresql://{info.user}:{password}@{info.host}:{info.port}/{info.dbname}"


def _migrator_database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://orchestrator:orchestrator@{info.host}:{info.port}/{info.dbname}"


def _runtime_database_url(postgresql) -> str:
    info = postgresql.info
    return f"postgresql://orchestrator_runtime:orchestrator_runtime@{info.host}:{info.port}/{info.dbname}"


def _apply_app_role_sql(postgresql) -> None:
    """Executa o script infra/postgres/10-app-role.sql usando a conexão administrativa."""
    sql_text = _APP_ROLE_SQL_PATH.read_text(encoding="utf-8")
    # Executa os blocos do script contra o banco de testes
    # Remove o comando \connect que é exclusivo do psql interativo
    commands = [cmd.strip() for cmd in sql_text.split("\n\\connect") if cmd.strip()]
    
    # Primeira parte (criação de roles e alter database)
    postgresql.execute(commands[0])
    postgresql.commit()
    
    # Segunda parte (grants e default privileges no schema public)
    if len(commands) > 1:
        # Remove o nome do banco se sobrou na linha seguinte
        second_part = commands[1]
        lines = second_part.splitlines()
        if lines and not lines[0].strip().endswith(";"):
            # Linha com o argumento do \connect (ex: "orchestrator")
            second_part = "\n".join(lines[1:])
        postgresql.execute(second_part)
        postgresql.commit()


def test_app_role_sql_idempotent_bootstrap_converts_legacy_volume(postgresql):
    """Prova que 10-app-role.sql converte um volume legado com orchestrator NOBYPASSRLS para BYPASSRLS."""
    # 1. Simula estado legado: role orchestrator é NOBYPASSRLS e proprietária do schema
    postgresql.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE orchestrator LOGIN PASSWORD 'orchestrator' NOSUPERUSER NOBYPASSRLS;
        EXCEPTION WHEN duplicate_object THEN
            ALTER ROLE orchestrator NOSUPERUSER NOBYPASSRLS;
        END
        $$;
        """
    )
    postgresql.commit()

    row_before = postgresql.execute(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'orchestrator'"
    ).fetchone()
    assert row_before == ("orchestrator", False, False)

    # 2. Executa o bootstrap do 10-app-role.sql
    _apply_app_role_sql(postgresql)

    # 3. Verifica os atributos pós-bootstrap
    row_migrator = postgresql.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'orchestrator'"
    ).fetchone()
    assert row_migrator == ("orchestrator", False, True, True), "orchestrator deve ser LOGIN NOSUPERUSER BYPASSRLS"

    row_runtime = postgresql.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, rolcreatedb, rolcreaterole, rolreplication "
        "FROM pg_roles WHERE rolname = 'orchestrator_runtime'"
    ).fetchone()
    assert row_runtime == ("orchestrator_runtime", False, False, True, False, False, False), (
        "orchestrator_runtime deve ser LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
    )

    # 4. Executa novamente para provar idempotência em volume já provisionado
    _apply_app_role_sql(postgresql)
    row_migrator_2 = postgresql.execute(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'orchestrator'"
    ).fetchone()
    assert row_migrator_2 == (True,)


def test_migrate_fail_fast_when_connected_with_nobypassrls(postgresql):
    """Prova que a migração 0012 falha rápido com mensagem explicativa se rodar com papel NOBYPASSRLS."""
    _apply_app_role_sql(postgresql)
    migrator_url = _migrator_database_url(postgresql)
    runtime_url = _runtime_database_url(postgresql)

    # Aplica até a revisão 0011 com o migrador
    upgrade_database(migrator_url, "20260804_0011")

    # Tenta aplicar 0012 com o runtime NOBYPASSRLS
    with pytest.raises(RuntimeError) as exc_info:
        upgrade_database(runtime_url, "20260829_0012")

    error_msg = str(exc_info.value)
    assert "MIGRATION_DATABASE_URL" in error_msg and "BYPASSRLS" in error_msg, (
        f"Esperava mensagem explicativa sobre BYPASSRLS/MIGRATION_DATABASE_URL, obteve: {error_msg}"
    )


def test_migrate_and_function_acls_with_bypassrls_migrator(postgresql):
    """Executa migração 0012 com orchestrator (BYPASSRLS) e valida ownership e ACLs dos helpers."""
    _apply_app_role_sql(postgresql)
    migrator_url = _migrator_database_url(postgresql)

    # Upgrade com o papel migrador
    upgrade_database(migrator_url)

    # Verifica que current_actor_role e organization_has_members foram criadas
    funcs = postgresql.execute(
        """
        SELECT proname, prosecdef, proconfig, r.rolname
        FROM pg_proc p
        JOIN pg_roles r ON r.oid = p.proowner
        WHERE proname IN ('current_actor_role', 'organization_has_members')
        ORDER BY proname
        """
    ).fetchall()
    assert len(funcs) == 2, f"Esperava 2 funções, encontrou {len(funcs)}"

    for proname, prosecdef, proconfig, owner in funcs:
        assert prosecdef is True, f"{proname} deve ser SECURITY DEFINER"
        assert owner == "orchestrator", f"{proname} deve pertencer ao migrador 'orchestrator'"
        assert "row_security=off" in (proconfig or []), f"{proname} deve ter SET row_security=off"
        assert "search_path=pg_catalog" in (proconfig or []), f"{proname} deve ter search_path=pg_catalog exato"
        assert not any("public" in c for c in (proconfig or [])), f"{proname} não deve incluir public no search_path"
        assert not any("pg_temp" in c for c in (proconfig or [])), f"{proname} não deve incluir pg_temp no search_path"

    # Verifica que PUBLIC não tem permissão EXECUTE
    public_privs = postgresql.execute(
        """
        SELECT proname, (aclexplode(proacl)).grantee = 0 AS is_public
        FROM pg_proc
        WHERE proname IN ('current_actor_role', 'organization_has_members')
          AND proacl IS NOT NULL
        """
    ).fetchall()
    for proname, is_public in public_privs:
        assert not is_public, f"{proname} não deve ter EXECUTE para PUBLIC"

    # Verifica que orchestrator_runtime tem permissão EXECUTE
    runtime_has_execute = postgresql.execute(
        """
        SELECT has_function_privilege('orchestrator_runtime', 'public.current_actor_role()', 'EXECUTE'),
               has_function_privilege('orchestrator_runtime', 'public.organization_has_members()', 'EXECUTE')
        """
    ).fetchone()
    assert runtime_has_execute == (True, True), "orchestrator_runtime deve ter EXECUTE nos helpers"


async def test_runtime_role_isolation_and_authorized_operations(postgresql):
    """Prova que orchestrator_runtime não consegue burlar RLS, mas executa operações autorizadas."""
    _apply_app_role_sql(postgresql)
    migrator_url = _migrator_database_url(postgresql)
    upgrade_database(migrator_url)

    # Seed inicial via conexão administrativa do migrador
    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    runtime_url = _runtime_database_url(postgresql)
    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")

    # 1. Tenta burlar RLS via SET row_security=off sob runtime role
    with psycopg.connect(runtime_url) as conn:
        conn.execute("SET row_security = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM organization_members")

    # 2. Database.open com runtime_url aceita conexão e autoriza tenant
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        repo = PostgresMemberRepository(db, owner_tenant)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")

        # Operação autorizada via policy
        member = await repo.grant_member(
            subject="access|member-1",
            role="member",
            actor_principal=owner_principal,
        )
        assert member.role == "member"

        members = await repo.list_members()
        assert len(members) == 2


def test_provision_runtime_role_grants_function_permissions(postgresql):
    """Prova que provision_runtime_role concede EXECUTE em funções e restaura senha para isolamento."""
    admin_url = _database_url(postgresql)
    _apply_app_role_sql(postgresql)
    migrator_url = _migrator_database_url(postgresql)
    runtime_url = _runtime_database_url(postgresql)
    upgrade_database(migrator_url)

    try:
        # Executa provision_runtime_role com a conexão administrativa
        provision_runtime_role(admin_url, "new_runtime_password")

        # Confirma que orchestrator_runtime tem permissões de execução
        runtime_has_execute = postgresql.execute(
            """
            SELECT has_function_privilege('orchestrator_runtime', 'public.current_actor_role()', 'EXECUTE'),
                   has_function_privilege('orchestrator_runtime', 'public.organization_has_members()', 'EXECUTE')
            """
        ).fetchone()
        assert runtime_has_execute == (True, True)
    finally:
        # Restaura a senha esperada do papel de runtime para não quebrar o cluster local
        with psycopg.connect(admin_url) as conn:
            conn.execute("ALTER ROLE orchestrator_runtime WITH PASSWORD 'orchestrator_runtime'")

    # Confirma que o runtime consegue autenticar com a senha padrão restaurada
    with psycopg.connect(runtime_url) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
