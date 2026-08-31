"""Testes de integração PostgreSQL com RLS real (NOBYPASSRLS), sem recursão e concorrência para membros."""
from __future__ import annotations

import asyncio
from typing import Optional

from orchestrator.auth import RequestPrincipal
from orchestrator.db import (
    Database,
    LastOwnerError,
    TenantIdentity,
    create_organization,
    grant_membership,
    upgrade_database,
)
from orchestrator.db.members import PostgresMemberRepository


def _database_url(postgresql) -> str:
    info = postgresql.info
    password = info.password or "postgres"
    return f"postgresql://{info.user}:{password}@{info.host}:{info.port}/{info.dbname}"


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
    postgresql.execute("ALTER ROLE tenant_app NOSUPERUSER NOBYPASSRLS")
    postgresql.execute("GRANT USAGE ON SCHEMA public TO tenant_app")
    postgresql.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tenant_app"
    )
    postgresql.execute(
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO tenant_app"
    )
    postgresql.commit()

    # Valida explicitamente que tenant_app é NOSUPERUSER e NOBYPASSRLS
    row = postgresql.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'tenant_app'"
    ).fetchone()
    assert row == (False, False), f"Expected NOSUPERUSER NOBYPASSRLS, got {row}"

    info = postgresql.info
    return f"postgresql://tenant_app:tenant_app@{info.host}:{info.port}/{info.dbname}"


def _seed_organization_and_owner(
    database_url: str,
    slug: str,
    name: str,
    subject: str,
    role: str = "owner",
    email: Optional[str] = None,
    display_name: Optional[str] = None,
) -> None:
    create_organization(database_url, slug=slug, name=name)
    grant_membership(database_url, organization_slug=slug, user_subject=subject, role=role)


async def test_postgres_member_repository_grant_update_revoke_with_nobypassrls(postgresql):
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)

    # Seed inicial via conexão administrativa do fixture
    _seed_organization_and_owner(
        database_url,
        slug="acme",
        name="Acme Inc.",
        subject="access|owner-1",
        role="owner",
        email="owner1@acme.test",
        display_name="Owner One",
    )

    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        repo = PostgresMemberRepository(db, owner_tenant)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")

        # 1. Owner adiciona novo member
        new_member = await repo.grant_member(
            subject="access|member-1",
            role="member",
            email="member1@acme.test",
            display_name="Member One",
            actor_principal=owner_principal,
        )
        assert new_member.role == "member"
        assert new_member.email == "member1@acme.test"

        # 2. Listagem de membros
        members = await repo.list_members()
        subjects = [m.subject for m in members]
        assert "access|owner-1" in subjects
        assert "access|member-1" in subjects

        # 3. Owner atualiza papel para viewer
        updated = await repo.update_member_role(
            subject="access|member-1",
            new_role="viewer",
            actor_principal=owner_principal,
        )
        assert updated.role == "viewer"

        # 4. Owner revoga member
        revoked = await repo.revoke_member("access|member-1", actor_principal=owner_principal)
        assert revoked is True

        members_after = await repo.list_members()
        assert "access|member-1" not in [m.subject for m in members_after]


async def test_postgres_member_repository_cross_tenant_isolation(postgresql):
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)

    # Seed inicial para acme e globex
    _seed_organization_and_owner(database_url, "acme", "Acme", "access|acme-owner", "owner")
    _seed_organization_and_owner(database_url, "globex", "Globex", "access|globex-owner", "owner")

    acme_identity = TenantIdentity("acme", "Acme", "access|acme-owner")
    globex_identity = TenantIdentity("globex", "Globex", "access|globex-owner")

    async with Database(runtime_url) as db:
        acme_tenant = await db.authorize_tenant(acme_identity)
        globex_tenant = await db.authorize_tenant(globex_identity)

        acme_repo = PostgresMemberRepository(db, acme_tenant)
        globex_repo = PostgresMemberRepository(db, globex_tenant)

        acme_principal = RequestPrincipal.from_tenant(acme_tenant, organization_name="Acme")
        globex_principal = RequestPrincipal.from_tenant(globex_tenant, organization_name="Globex")

        await acme_repo.grant_member("access|acme-user", "member", actor_principal=acme_principal)
        await globex_repo.grant_member("access|globex-user", "member", actor_principal=globex_principal)

        acme_members = [m.subject for m in await acme_repo.list_members()]
        globex_members = [m.subject for m in await globex_repo.list_members()]

        assert "access|acme-user" in acme_members
        assert "access|globex-user" not in acme_members

        assert "access|globex-user" in globex_members
        assert "access|acme-user" not in globex_members


async def test_postgres_member_repository_admin_role_restrictions(postgresql):
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)

    # Seed do owner inicial
    _seed_organization_and_owner(database_url, "acme", "Acme", "access|owner-1", "owner")

    owner_identity = TenantIdentity("acme", "Acme", "access|owner-1")
    admin_identity = TenantIdentity("acme", "Acme", "access|admin-1")

    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_repo = PostgresMemberRepository(db, owner_tenant)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme")

        # Owner cria admin
        await owner_repo.grant_member("access|admin-1", "admin", actor_principal=owner_principal)

        admin_tenant = await db.authorize_tenant(admin_identity)
        admin_repo = PostgresMemberRepository(db, admin_tenant)
        admin_principal = RequestPrincipal.from_tenant(admin_tenant, organization_name="Acme")

        # Admin não pode criar owner
        try:
            await admin_repo.grant_member("access|hacker", "owner", actor_principal=admin_principal)
            raise AssertionError("Admin não deve criar owner")
        except PermissionError:
            pass

        # Admin não pode rebaixar owner
        try:
            await admin_repo.update_member_role("access|owner-1", "member", actor_principal=admin_principal)
            raise AssertionError("Admin não deve rebaixar owner")
        except (PermissionError, KeyError):
            pass

        # Admin pode criar member e viewer
        m = await admin_repo.grant_member("access|new-member", "member", actor_principal=admin_principal)
        assert m.role == "member"


async def test_postgres_member_repository_last_owner_concurrency(postgresql):
    """Duas transações simultâneas tentando remover/rebaixar owners diferentes: serializadas no lock da Organization."""
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)

    _seed_organization_and_owner(database_url, "acme", "Acme", "access|owner-1", "owner")
    _seed_organization_and_owner(database_url, "acme", "Acme", "access|owner-2", "owner")

    owner1_id = TenantIdentity("acme", "Acme", "access|owner-1")
    owner2_id = TenantIdentity("acme", "Acme", "access|owner-2")

    async with Database(runtime_url) as db:
        owner1_tenant = await db.authorize_tenant(owner1_id)
        owner2_tenant = await db.authorize_tenant(owner2_id)

        owner1_repo = PostgresMemberRepository(db, owner1_tenant)
        owner2_repo = PostgresMemberRepository(db, owner2_tenant)

        owner1_principal = RequestPrincipal.from_tenant(owner1_tenant, organization_name="Acme")
        owner2_principal = RequestPrincipal.from_tenant(owner2_tenant, organization_name="Acme")

        # Ambas as tarefas tentam rebaixar o respectivo owner simultaneamente
        results = []
        errors = []

        async def demote_owner_1():
            try:
                res = await owner1_repo.update_member_role("access|owner-1", "member", actor_principal=owner1_principal)
                results.append(("owner-1", res))
            except LastOwnerError as e:
                errors.append(("owner-1", e))

        async def demote_owner_2():
            try:
                res = await owner2_repo.update_member_role("access|owner-2", "member", actor_principal=owner2_principal)
                results.append(("owner-2", res))
            except LastOwnerError as e:
                errors.append(("owner-2", e))

        await asyncio.gather(demote_owner_1(), demote_owner_2())

        # Exatamente uma transação deve ter sucedido e a outra falhado com LastOwnerError
        assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
        assert len(errors) == 1, f"Expected exactly 1 LastOwnerError, got {len(errors)}"

        # A organização deve terminar com exatamente um owner
        members = await owner1_repo.list_members()
        owners = [m for m in members if m.role == "owner"]
        assert len(owners) == 1


async def test_postgres_ensure_tenant_bootstrap_with_nobypassrls(postgresql):
    """Garante que ensure_tenant (modo local / disabled) consegue materializar o primeiro owner sem violar RLS."""
    database_url = _database_url(postgresql)
    upgrade_database(database_url)
    runtime_url = _runtime_database_url(postgresql)

    identity = TenantIdentity("fresh-org", "Fresh Organization", "access|first-owner")

    async with Database(runtime_url) as db:
        tenant = await db.ensure_tenant(identity)
        assert tenant.organization_slug == "fresh-org"

        # Segunda chamada deve ser idempotente sob RLS
        tenant2 = await db.ensure_tenant(identity)
        assert tenant2 == tenant

        repo = PostgresMemberRepository(db, tenant)
        members = await repo.list_members()
        assert len(members) == 1
        assert members[0].subject == "access|first-owner"
        assert members[0].role == "owner"


def test_postgres_migration_0012_upgrade_downgrade(postgresql):
    """Testa ciclo completo de upgrade e downgrade da migração 20260829_0012."""
    database_url = _database_url(postgresql)

    # Upgrade to head
    upgrade_database(database_url, "head")

    # Downgrade to 0011
    upgrade_database(database_url, "20260804_0011")

    # Upgrade back to head
    upgrade_database(database_url, "head")
