"""Testes da fundação de RBAC, RequestPrincipal e Database.authorize_tenant."""
from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from orchestrator.auth import (
    ROLE_PERMISSIONS,
    Permission,
    RequestPrincipal,
    get_current_principal,
    request_principal_context,
)
from orchestrator.db import TenantContext, TenantIdentity
from orchestrator.db.database import Database


def test_role_permission_matrix():
    """Valida que viewer é read-only, member opera campanhas/prompts, admin gerencia membros (sem owner/admin) e owner tem tudo."""
    viewer_perms = ROLE_PERMISSIONS["viewer"]
    member_perms = ROLE_PERMISSIONS["member"]
    admin_perms = ROLE_PERMISSIONS["admin"]
    owner_perms = ROLE_PERMISSIONS["owner"]

    assert viewer_perms == frozenset({Permission.READ})
    
    assert Permission.READ in member_perms
    assert Permission.RUNS_CREATE in member_perms
    assert Permission.RUNS_REVIEW in member_perms
    assert Permission.RUNS_RETRY in member_perms
    assert Permission.RUNS_VOICE_REROLL in member_perms
    assert Permission.PROMPTS_WRITE in member_perms
    assert Permission.MEMBERS_READ not in member_perms
    assert Permission.MEMBERS_WRITE not in member_perms

    assert Permission.MEMBERS_READ in admin_perms
    assert Permission.MEMBERS_WRITE in admin_perms
    assert Permission.RUNS_CREATE in admin_perms

    assert Permission.MEMBERS_READ in owner_perms
    assert Permission.MEMBERS_WRITE in owner_perms
    assert Permission.RUNS_CREATE in owner_perms


def test_request_principal_permissions_and_role_management():
    tenant = TenantContext(
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        organization_slug="acme",
        user_subject="user-1",
        role="viewer",
    )
    viewer_principal = RequestPrincipal.from_tenant(
        tenant,
        organization_name="Acme Inc.",
        claims={"email": "viewer@acme.com", "name": "Viewer User", "sensitive_token": "secret"},
    )

    assert viewer_principal.has_permission(Permission.READ)
    assert not viewer_principal.has_permission(Permission.RUNS_CREATE)
    assert not viewer_principal.has_permission(Permission.MEMBERS_WRITE)
    assert not viewer_principal.can_manage_role("viewer")
    assert not viewer_principal.can_manage_role("member")
    assert not viewer_principal.can_manage_role("admin")
    assert not viewer_principal.can_manage_role("owner")

    # Garante que claims sensíveis são descartados do perfil público
    assert "sensitive_token" not in viewer_principal.claims
    assert viewer_principal.claims.get("email") == "viewer@acme.com"
    assert viewer_principal.claims.get("name") == "Viewer User"

    admin_tenant = TenantContext(
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000003"),
        organization_slug="acme",
        user_subject="admin-1",
        role="admin",
    )
    admin_principal = RequestPrincipal.from_tenant(admin_tenant, organization_name="Acme Inc.")
    assert admin_principal.has_permission(Permission.MEMBERS_WRITE)
    assert admin_principal.can_manage_role("viewer")
    assert admin_principal.can_manage_role("member")
    assert not admin_principal.can_manage_role("admin")
    assert not admin_principal.can_manage_role("owner")

    owner_tenant = TenantContext(
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000004"),
        organization_slug="acme",
        user_subject="owner-1",
        role="owner",
    )
    owner_principal = RequestPrincipal.from_tenant(owner_tenant, organization_name="Acme Inc.")
    assert owner_principal.can_manage_role("viewer")
    assert owner_principal.can_manage_role("member")
    assert owner_principal.can_manage_role("admin")
    assert owner_principal.can_manage_role("owner")


async def test_principal_context_var_is_isolated_between_coroutines():
    tenant_a = TenantContext(
        organization_id=uuid4(),
        user_id=uuid4(),
        organization_slug="org-a",
        user_subject="user-a",
        role="member",
    )
    principal_a = RequestPrincipal.from_tenant(tenant_a, organization_name="Org A")

    tenant_b = TenantContext(
        organization_id=uuid4(),
        user_id=uuid4(),
        organization_slug="org-b",
        user_subject="user-b",
        role="admin",
    )
    principal_b = RequestPrincipal.from_tenant(tenant_b, organization_name="Org B")

    async def worker(principal: RequestPrincipal):
        with request_principal_context(principal):
            await asyncio.sleep(0.01)
            current = get_current_principal()
            assert current == principal
            return current.user_subject

    results = await asyncio.gather(worker(principal_a), worker(principal_b))
    assert results == ["user-a", "user-b"]


def test_deterministic_local_principal_in_disabled_mode(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "disabled")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "local-slug")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Local Name")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "local-subject")

    principal = get_current_principal()
    assert principal.organization_slug == "local-slug"
    assert principal.organization_name == "Local Name"
    assert principal.user_subject == "local-subject"
    assert principal.role == "owner"
    assert principal.has_permission(Permission.RUNS_CREATE)
    assert principal.has_permission(Permission.MEMBERS_WRITE)


async def test_database_authorize_tenant_preserves_role():
    """Database.authorize_tenant deve extrair e devolver o role real ('member', 'admin', 'viewer', etc.)."""
    identity = TenantIdentity("acme", "Acme Inc.", "access|bob")
    
    class FakeCursor:
        async def fetchone(self):
            return ("admin",)

    class FakeConnection:
        async def execute(self, query, params):
            return FakeCursor()

    class FakePoolConnection:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_exc):
            pass

    class FakePool:
        def connection(self):
            return FakePoolConnection()

    database = Database.__new__(Database)
    database._pool = FakePool()

    tenant = await database.authorize_tenant(identity)
    assert tenant.role == "admin"
    assert tenant.organization_slug == "acme"
    assert tenant.user_subject == "access|bob"
