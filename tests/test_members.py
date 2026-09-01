"""Testes para o repositório e endpoints de gestão de membros (/api/v2/members)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.auth import CloudflareAccessMiddleware
from orchestrator.db import TenantContext, TenantIdentity
from orchestrator.db.members import _IN_MEMORY_REPOSITORIES
from orchestrator.web.server import app as main_app


@pytest.fixture
def make_test_app(monkeypatch):
    _IN_MEMORY_REPOSITORIES.clear()
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")

    def _make(role: str, subject: str = "caller"):
        tenant = TenantContext(
            organization_id=TenantIdentity("acme", "Acme", subject).context().organization_id,
            user_id=TenantIdentity("acme", "Acme", subject).context().user_id,
            organization_slug="acme",
            user_subject=subject,
            role=role,
        )

        async def authorize(_identity: TenantIdentity) -> TenantContext:
            return tenant

        class Verifier:
            async def verify(self, _token: str):
                return {"sub": subject}

        test_app = FastAPI()
        test_app.add_middleware(
            CloudflareAccessMiddleware,
            verifier=Verifier(),
            authorize=authorize,
        )
        for route in main_app.routes:
            test_app.routes.append(route)
        return test_app

    return _make


async def test_members_list_permissions(make_test_app):
    headers = {
        "Cf-Access-Jwt-Assertion": "token",
    }

    # Viewer e Member recebem 403
    for role in ("viewer", "member"):
        app = make_test_app(role)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://origin.test") as client:
            resp = await client.get("/api/v2/members", headers=headers)
            assert resp.status_code == 403, f"Expected 403 for {role}, got {resp.status_code}"

    # Admin e Owner recebem 200
    for role in ("admin", "owner"):
        app = make_test_app(role)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://origin.test") as client:
            resp = await client.get("/api/v2/members", headers=headers)
            assert resp.status_code == 200, f"Expected 200 for {role}, got {resp.status_code}"
            assert "members" in resp.json()


async def test_admin_cannot_grant_or_alter_owner_or_admin(make_test_app):
    headers = {
        "Cf-Access-Jwt-Assertion": "token",
    }
    admin_app = make_test_app("admin", subject="admin-user")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="https://origin.test") as client:
        # Admin tentando conceder owner -> 403
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "new-user-1", "role": "owner"},
            headers=headers,
        )
        assert resp.status_code == 403

        # Admin tentando conceder admin -> 403
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "new-user-2", "role": "admin"},
            headers=headers,
        )
        assert resp.status_code == 403

        # Admin concedendo member -> 200
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "new-user-3", "role": "member", "email": "new@example.com"},
            headers=headers,
        )
        assert resp.status_code in (200, 201)

        # Admin concedendo viewer -> 200
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "new-user-4", "role": "viewer"},
            headers=headers,
        )
        assert resp.status_code in (200, 201)


async def test_post_member_rejects_existing_membership_with_409(make_test_app):
    """POST cria membership apenas se não existir; se existir, falha com 409 sem alterar estado."""
    headers = {"Cf-Access-Jwt-Assertion": "token"}
    owner_app = make_test_app("owner", subject="owner-user")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="https://origin.test") as client:
        # 1. Cria usuário com viewer
        resp1 = await client.post(
            "/api/v2/members",
            json={"subject": "target-user", "role": "viewer"},
            headers=headers,
        )
        assert resp1.status_code in (200, 201)

        # 2. Tenta fazer POST novamente com outro papel -> 409
        resp2 = await client.post(
            "/api/v2/members",
            json={"subject": "target-user", "role": "member"},
            headers=headers,
        )
        assert resp2.status_code == 409
        assert "já existe" in resp2.json().get("detail", "").lower()

        # 3. Verifica que papel continua viewer
        members_resp = await client.get("/api/v2/members", headers=headers)
        members = {m["subject"]: m["role"] for m in members_resp.json()["members"]}
        assert members["target-user"] == "viewer"

        # 4. Alteração de papel só é permitida via PATCH
        patch_resp = await client.patch(
            "/api/v2/members/target-user",
            json={"role": "member"},
            headers=headers,
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["member"]["role"] == "member"


async def test_admin_and_owner_cannot_demote_via_post_relying_on_409(make_test_app):
    """Admin ou owner tentando rebaixar usuário existente por POST falham com 409 sem alterar nada."""
    headers = {"Cf-Access-Jwt-Assertion": "token"}
    admin_app = make_test_app("admin", subject="admin-actor")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="https://origin.test") as client:
        # Admin tenta POST rebaixando o owner caller -> 409 (ou 403 se valida papel antes) e não altera
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "admin-actor", "role": "viewer"},
            headers=headers,
        )
        assert resp.status_code == 409


async def test_protect_last_owner_on_role_change_and_revocation(make_test_app):
    headers = {
        "Cf-Access-Jwt-Assertion": "token",
    }
    owner_app = make_test_app("owner", subject="owner-user")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="https://origin.test") as client:
        # Rebaixar o único owner existente deve falhar com 409 Conflict
        resp = await client.patch(
            "/api/v2/members/owner-user",
            json={"role": "member"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert "último owner" in resp.json().get("detail", "").lower() or "last owner" in resp.json().get("detail", "").lower()

        # Revogar o único owner existente deve falhar com 409 Conflict
        resp = await client.delete(
            "/api/v2/members/owner-user",
            headers=headers,
        )
        assert resp.status_code == 409


async def test_member_request_schemas_forbid_extra_fields(make_test_app):
    headers = {
        "Cf-Access-Jwt-Assertion": "token",
    }
    owner_app = make_test_app("owner", subject="owner-user")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="https://origin.test") as client:
        resp = await client.post(
            "/api/v2/members",
            json={"subject": "user-x", "role": "member", "unknown_field": "injected"},
            headers=headers,
        )
        assert resp.status_code == 422


async def test_concurrent_demote_last_owner_in_memory(make_test_app):
    """Duas requisições simultâneas tentando rebaixar owners diferentes: exatamente uma pode suceder e resta >= 1 owner."""
    import asyncio
    headers = {"Cf-Access-Jwt-Assertion": "token"}
    owner_app = make_test_app("owner", subject="owner-1")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="https://origin.test") as client:
        # Adiciona segundo owner
        grant_resp = await client.post(
            "/api/v2/members",
            json={"subject": "owner-2", "role": "owner"},
            headers=headers,
        )
        assert grant_resp.status_code in (200, 201)

        # Tentativa concorrente de rebaixar owner-1 e owner-2
        resp1, resp2 = await asyncio.gather(
            client.patch("/api/v2/members/owner-1", json={"role": "member"}, headers=headers),
            client.patch("/api/v2/members/owner-2", json={"role": "member"}, headers=headers),
        )

        status_codes = [resp1.status_code, resp2.status_code]
        assert 200 in status_codes
        assert 409 in status_codes

        members_resp = await client.get("/api/v2/members", headers=headers)
        owners = [m for m in members_resp.json()["members"] if m["role"] == "owner"]
        assert len(owners) == 1
