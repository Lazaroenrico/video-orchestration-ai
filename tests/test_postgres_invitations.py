"""Testes de integração PostgreSQL para o ciclo de vida de convites e claim atômico."""
from __future__ import annotations

import pathlib

import pytest

from orchestrator.auth import RequestPrincipal
from orchestrator.db import (
    Database,
    InvitationConflictError,
    PostgresInvitationRepository,
    PostgresMemberRepository,
    TenantAuthorizationError,
    TenantIdentity,
    create_organization,
    grant_membership,
    owner_bootstrap,
    upgrade_database,
)

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
    sql_text = _APP_ROLE_SQL_PATH.read_text(encoding="utf-8")
    commands = [cmd.strip() for cmd in sql_text.split("\n\\connect") if cmd.strip()]
    postgresql.execute(commands[0])
    postgresql.commit()
    if len(commands) > 1:
        second_part = commands[1]
        lines = second_part.splitlines()
        if lines and not lines[0].strip().endswith(";"):
            second_part = "\n".join(lines[1:])
        postgresql.execute(second_part)
        postgresql.commit()


@pytest.fixture
def bootstrapped_db(postgresql):
    _apply_app_role_sql(postgresql)
    migrator_url = _migrator_database_url(postgresql)
    upgrade_database(migrator_url)
    return {
        "migrator_url": migrator_url,
        "runtime_url": _runtime_database_url(postgresql),
        "admin_url": _database_url(postgresql),
    }


def test_migration_0013_creates_table_and_claim_function(bootstrapped_db, postgresql):
    funcs = postgresql.execute(
        """
        SELECT proname, prosecdef, proconfig, r.rolname
        FROM pg_proc p
        JOIN pg_roles r ON r.oid = p.proowner
        WHERE proname = 'claim_organization_invitation'
        """
    ).fetchall()
    assert len(funcs) == 1
    proname, prosecdef, proconfig, owner = funcs[0]
    assert prosecdef is True
    assert owner == "orchestrator"
    assert "row_security=off" in (proconfig or [])
    assert "search_path=pg_catalog" in (proconfig or [])


async def test_invitations_crud_and_rbac(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")
        repo = PostgresInvitationRepository(db, owner_tenant)

        # 1. Cria convite como owner
        inv = await repo.create_invitation(
            email="  Bob@Acme.COM ",
            role="member",
            actor_principal=owner_principal,
        )
        assert inv.normalized_email == "bob@acme.com"
        assert inv.role == "member"
        assert inv.invited_by_user_id == owner_tenant.user_id

        # 2. Conflito: não permite convidar o mesmo e-mail novamente
        with pytest.raises(InvitationConflictError, match="já existe um convite pendente"):
            await repo.create_invitation(
                email="BOB@acme.com",
                role="admin",
                actor_principal=owner_principal,
            )

        # 3. Lista convites
        invs = await repo.list_invitations()
        assert len(invs) == 1
        assert invs[0].normalized_email == "bob@acme.com"

        # 4. Cancela convite
        cancelled = await repo.cancel_invitation("bob@acme.com", actor_principal=owner_principal)
        assert cancelled is True
        invs_after = await repo.list_invitations()
        assert len(invs_after) == 0

        # Cancelar convite inexistente retorna False
        assert await repo.cancel_invitation("bob@acme.com", actor_principal=owner_principal) is False


async def test_invitation_conflict_when_user_already_member(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")
        members_repo = PostgresMemberRepository(db, owner_tenant)
        invitations_repo = PostgresInvitationRepository(db, owner_tenant)

        # Adiciona membro ativo com e-mail
        await members_repo.grant_member(
            subject="access|alice",
            role="member",
            actor_principal=owner_principal,
            email="alice@acme.com",
        )

        # Tentar convidar alice@acme.com deve falhar com InvitationConflictError
        with pytest.raises(InvitationConflictError, match="já pertence a um membro ativo"):
            await invitations_repo.create_invitation(
                email="Alice@Acme.com",
                role="viewer",
                actor_principal=owner_principal,
            )


async def test_authorize_tenant_claims_invitation_atomically(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")
        inv_repo = PostgresInvitationRepository(db, owner_tenant)

        # Cria convite para carlos@acme.com como admin
        await inv_repo.create_invitation(
            email="Carlos@Acme.com",
            role="admin",
            actor_principal=owner_principal,
        )

        # 1. Carlos faz primeiro login via Cloudflare Access com verified_email
        carlos_identity = TenantIdentity("acme", "Acme Inc.", "access|carlos-sub")
        carlos_tenant = await db.authorize_tenant(carlos_identity, verified_email="carlos@acme.com")
        assert carlos_tenant.role == "admin"
        assert carlos_tenant.organization_slug == "acme"
        assert carlos_tenant.user_subject == "access|carlos-sub"

        # 2. Convite foi consumido e não está mais pendente
        invs = await inv_repo.list_invitations()
        assert len(invs) == 0

        # 3. Requisições subsequentes de Carlos autorizam sem precisar de verified_email
        carlos_tenant_subsequent = await db.authorize_tenant(carlos_identity, verified_email=None)
        assert carlos_tenant_subsequent.role == "admin"


async def test_authorize_tenant_rejects_uninvited_user(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    async with Database(runtime_url) as db:
        intruder_identity = TenantIdentity("acme", "Acme Inc.", "access|intruder")

        # Sem e-mail e sem membership -> 403 / TenantAuthorizationError
        with pytest.raises(TenantAuthorizationError):
            await db.authorize_tenant(intruder_identity, verified_email=None)

        # Com e-mail mas sem convite correspondente -> 403 / TenantAuthorizationError
        with pytest.raises(TenantAuthorizationError):
            await db.authorize_tenant(intruder_identity, verified_email="intruder@external.com")


async def test_concurrent_invitation_claims(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme", name="Acme Inc.")
    grant_membership(migrator_url, organization_slug="acme", user_subject="access|owner-1", role="owner")

    owner_identity = TenantIdentity("acme", "Acme Inc.", "access|owner-1")
    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")
        inv_repo = PostgresInvitationRepository(db, owner_tenant)

        # Cria convite para daniel@acme.com
        await inv_repo.create_invitation(
            email="daniel@acme.com",
            role="member",
            actor_principal=owner_principal,
        )

        daniel_identity = TenantIdentity("acme", "Acme Inc.", "access|daniel-sub")

        # Dispara 10 claims simultâneos da mesma identidade
        import asyncio

        async def claim_task():
            return await db.authorize_tenant(daniel_identity, verified_email="daniel@acme.com")

        results = await asyncio.gather(*[claim_task() for _ in range(10)], return_exceptions=True)
        # Todos devem retornar TenantContext com role="member" (sem exceções)
        for r in results:
            assert not isinstance(r, Exception), f"Falha no claim concorrente: {r}"
            assert r.role == "member"

        # Verifica que o convite foi removido e há exatamente 1 membership para Daniel
        invs = await inv_repo.list_invitations()
        assert len(invs) == 0

        members_repo = PostgresMemberRepository(db, owner_tenant)
        members = await members_repo.list_members()
        daniel_members = [m for m in members if m.subject == "access|daniel-sub"]
        assert len(daniel_members) == 1
        assert daniel_members[0].role == "member"


async def test_owner_bootstrap_lifecycle_and_fail_closed(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    # 1. Primeiro bootstrap em organização vazia cria convite owner
    res1 = owner_bootstrap(
        migrator_url,
        organization_slug="bootstrap-org",
        organization_name="Bootstrap Org",
        owner_email="Owner@Bootstrap.ORG ",
    )
    assert res1["status"] == "invitation_created"
    assert res1["email"] == "owner@bootstrap.org"
    assert res1["role"] == "owner"

    # 2. Executar novamente com convite pendente é idempotente
    res2 = owner_bootstrap(
        migrator_url,
        organization_slug="bootstrap-org",
        organization_name="Bootstrap Org",
        owner_email="owner@bootstrap.org",
    )
    assert res2["status"] == "invitation_pending"

    # 3. Owner aceita o convite no primeiro login
    async with Database(runtime_url) as db:
        owner_identity = TenantIdentity("bootstrap-org", "Bootstrap Org", "access|founder-owner")
        owner_tenant = await db.authorize_tenant(owner_identity, verified_email="owner@bootstrap.org")
        assert owner_tenant.role == "owner"

    # 4. Novo bootstrap com owner já estabelecido reconhece o estado
    res3 = owner_bootstrap(
        migrator_url,
        organization_slug="bootstrap-org",
        organization_name="Bootstrap Org",
        owner_email="owner@bootstrap.org",
    )
    assert res3["status"] == "already_established"

    # 5. Tentativa de bootstrap para um e-mail diferente quando a organização já tem membros falha fechada
    with pytest.raises(RuntimeError, match="já possui 1 membro.*membership-grant"):
        owner_bootstrap(
            migrator_url,
            organization_slug="bootstrap-org",
            organization_name="Bootstrap Org",
            owner_email="intruder@bootstrap.org",
        )


async def test_claim_function_enforces_exact_session_context_equality(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme-sec", name="Acme Security")
    owner_identity = TenantIdentity("acme-sec", "Acme Security", "access|owner-1")
    grant_membership(migrator_url, organization_slug="acme-sec", user_subject="access|owner-1", role="owner")

    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Security")
        repo = PostgresInvitationRepository(db, owner_tenant)

        await repo.create_invitation(
            email="target@acme-sec.com",
            role="member",
            actor_principal=owner_principal,
        )

        attacker_identity = TenantIdentity("acme-sec", "Acme Security", "access|attacker")
        attacker_tenant = attacker_identity.context()

        # Tentativa de chamar claim passando organization_id falso ou user_id de outra pessoa
        from uuid import uuid4
        fake_org_id = uuid4()
        fake_user_id = uuid4()

        async with db.connection(attacker_tenant) as connection:
            with pytest.raises(Exception, match="organization_id mismatch with session context"):
                await connection.execute(
                    "SELECT public.claim_organization_invitation(%s, %s, %s, %s, %s)",
                    (fake_org_id, attacker_tenant.user_id, attacker_tenant.user_subject, "target@acme-sec.com", None),
                )

        async with db.connection(attacker_tenant) as connection:
            with pytest.raises(Exception, match="user_id mismatch with session context"):
                await connection.execute(
                    "SELECT public.claim_organization_invitation(%s, %s, %s, %s, %s)",
                    (attacker_tenant.organization_id, fake_user_id, attacker_tenant.user_subject, "target@acme-sec.com", None),
                )


async def test_invitations_select_policy_requires_owner_or_admin(bootstrapped_db):
    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme-rls", name="Acme RLS")
    grant_membership(migrator_url, organization_slug="acme-rls", user_subject="access|owner", role="owner")
    grant_membership(migrator_url, organization_slug="acme-rls", user_subject="access|member", role="member")

    owner_identity = TenantIdentity("acme-rls", "Acme RLS", "access|owner")
    member_identity = TenantIdentity("acme-rls", "Acme RLS", "access|member")

    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme RLS")
        repo = PostgresInvitationRepository(db, owner_tenant)

        await repo.create_invitation(
            email="invitee@acme-rls.com",
            role="viewer",
            actor_principal=owner_principal,
        )

        # Owner pode listar convites diretamente
        owner_invs = await repo.list_invitations()
        assert len(owner_invs) == 1

        # Member comum sob RLS não pode fazer SELECT direto na tabela organization_invitations
        member_tenant = await db.authorize_tenant(member_identity)
        member_repo = PostgresInvitationRepository(db, member_tenant)
        member_invs = await member_repo.list_invitations()
        assert len(member_invs) == 0


async def test_concurrent_create_invitation_duplicate_conflict(bootstrapped_db):
    import asyncio

    migrator_url = bootstrapped_db["migrator_url"]
    runtime_url = bootstrapped_db["runtime_url"]

    create_organization(migrator_url, slug="acme-race", name="Acme Race")
    grant_membership(migrator_url, organization_slug="acme-race", user_subject="access|owner", role="owner")
    owner_identity = TenantIdentity("acme-race", "Acme Race", "access|owner")

    async with Database(runtime_url) as db:
        owner_tenant = await db.authorize_tenant(owner_identity)
        owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Race")
        repo = PostgresInvitationRepository(db, owner_tenant)

        async def try_create():
            return await repo.create_invitation(
                email="duplicate@acme-race.com",
                role="member",
                actor_principal=owner_principal,
            )

        results = await asyncio.gather(try_create(), try_create(), return_exceptions=True)
        successes = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, InvitationConflictError)]

        assert len(successes) == 1
        assert len(conflicts) == 1
        assert "já existe um convite pendente" in str(conflicts[0])


def test_concurrent_owner_bootstrap_with_different_emails(bootstrapped_db):
    import concurrent.futures

    migrator_url = bootstrapped_db["migrator_url"]

    def run_bootstrap(email: str):
        return owner_bootstrap(
            migrator_url,
            organization_slug="race-org",
            organization_name="Race Org",
            owner_email=email,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_bootstrap, "owner-a@race-org.com")
        f2 = executor.submit(run_bootstrap, "owner-b@race-org.com")

        res1 = None
        res2 = None
        err1 = None
        err2 = None
        try:
            res1 = f1.result()
        except Exception as e:
            err1 = e
        try:
            res2 = f2.result()
        except Exception as e:
            err2 = e

    successes = [r for r in (res1, res2) if r is not None]
    errors = [e for e in (err1, err2) if e is not None]

    assert len(successes) == 1
    assert len(errors) == 1
    assert successes[0]["status"] == "invitation_created"
    assert "já possui um convite de owner pendente" in str(errors[0])



