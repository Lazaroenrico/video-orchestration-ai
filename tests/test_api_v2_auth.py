"""Testes de integração para RBAC nas rotas da API v2 e /api/v2/me."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.auth import (
    CloudflareAccessMiddleware,
)
from orchestrator.db import TenantContext, TenantIdentity
from orchestrator.web.server import app as main_app


@pytest.fixture
def mock_access_verifier():
    class DummyVerifier:
        def __init__(self, subject="access|alice", email="alice@example.com", name="Alice User"):
            self.subject = subject
            self.email = email
            self.name = name

        async def verify(self, token: str):
            if token == "invalid-jwt":
                from orchestrator.auth import AccessTokenError
                raise AccessTokenError("Token inválido")
            return {
                "sub": self.subject,
                "email": self.email,
                "name": self.name,
                "iss": "https://team.cloudflareaccess.com",
                "aud": "app-aud",
            }

    return DummyVerifier()


async def test_get_me_in_disabled_auth_mode(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "disabled")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "local-org")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Local Organization")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "local-user")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get("/api/v2/me")

    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "local-user"
    assert data["role"] == "owner"
    assert data["organization"]["slug"] == "local-org"
    assert data["organization"]["name"] == "Local Organization"
    assert "runs:create" in data["permissions"]
    assert "members:write" in data["permissions"]


async def test_get_me_in_cloudflare_access_mode(monkeypatch, mock_access_verifier):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")

    async def authorize_member(identity: TenantIdentity) -> TenantContext:
        tenant = identity.context()
        return TenantContext(
            organization_id=tenant.organization_id,
            user_id=tenant.user_id,
            organization_slug=tenant.organization_slug,
            user_subject=tenant.user_subject,
            role="member",
        )

    test_app = FastAPI()
    test_app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=mock_access_verifier,
        authorize=authorize_member,
    )
    for route in main_app.routes:
        test_app.routes.append(route)

    headers = {
        "Cf-Access-Jwt-Assertion": "valid-token",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get("/api/v2/me", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["subject"] == "access|alice"
    assert data["email"] == "alice@example.com"
    assert data["display_name"] == "Alice User"
    assert data["role"] == "member"
    assert data["auth_mode"] == "cloudflare_access"
    assert "runs:create" in data["permissions"]
    assert "members:write" not in data["permissions"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/v2/runs", {"campaign": {"offer": "test", "audience": "all", "batch_size": 2}}),
        ("POST", "/api/run", {"offer": "test", "batch": 2}),
        ("POST", "/api/run/web-1234/retry", {}),
        ("POST", "/api/v2/runs/web-1234/review", {"action": "approve"}),
        ("POST", "/api/approve/web-1234", {"approved": ["c1"]}),
        ("POST", "/api/approve/web-1234/concepts", {"concepts": []}),
        ("POST", "/api/approve/web-1234/creators/c1/reroll-voice", {}),
        ("POST", "/api/prompts", {"kind": "creator", "title": "t", "text": "p"}),
        ("DELETE", "/api/prompts/1", {}),
        ("POST", "/api/v2/members", {"subject": "user-2", "role": "member"}),
        ("PATCH", "/api/v2/members/user-2", {"role": "viewer"}),
        ("DELETE", "/api/v2/members/user-2", {}),
    ],
)
async def test_viewer_receives_403_on_all_mutations(monkeypatch, method, path, payload):
    """Garante que role 'viewer' é barrado com 403 Forbidden em TODAS as rotas de mutação."""
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme")

    tenant = TenantContext(
        organization_id=TenantIdentity("acme", "Acme", "viewer-user").context().organization_id,
        user_id=TenantIdentity("acme", "Acme", "viewer-user").context().user_id,
        organization_slug="acme",
        user_subject="viewer-user",
        role="viewer",
    )

    async def authorize_viewer(_identity: TenantIdentity) -> TenantContext:
        return tenant

    class Verifier:
        async def verify(self, _token: str):
            return {"sub": "viewer-user"}

    test_app = FastAPI()
    test_app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=authorize_viewer,
    )
    for route in main_app.routes:
        test_app.routes.append(route)

    headers = {
        "Cf-Access-Jwt-Assertion": "valid-token",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="https://origin.test",
    ) as client:
        if method == "POST":
            response = await client.post(path, json=payload, headers=headers)
        elif method == "PATCH":
            response = await client.patch(path, json=payload, headers=headers)
        elif method == "DELETE":
            response = await client.delete(path, headers=headers)

    assert response.status_code == 403, f"Expected 403 for {method} {path}, got {response.status_code}: {response.text}"


async def test_invitations_api_crud_flow_as_owner(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "disabled")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "local-inv-org")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Local Inv Org")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "local-owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main_app),
        base_url="https://origin.test",
    ) as client:
        # 1. Cria convite
        res = await client.post(
            "/api/v2/invitations",
            json={"email": "NewMember@Test.org ", "role": "member"},
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["ok"] is True
        assert data["invitation"]["email"] == "newmember@test.org"
        assert data["invitation"]["role"] == "member"

        # 2. Conflito em convite duplicado
        res_dup = await client.post(
            "/api/v2/invitations",
            json={"email": "newmember@test.org", "role": "admin"},
        )
        assert res_dup.status_code == 409

        # 3. Lista convites
        res_list = await client.get("/api/v2/invitations")
        assert res_list.status_code == 200
        invs = res_list.json()["invitations"]
        assert any(i["email"] == "newmember@test.org" for i in invs)

        # 4. Cancela convite
        res_del = await client.delete("/api/v2/invitations/newmember@test.org")
        assert res_del.status_code == 200

        # 5. Cancela novamente -> 404
        res_del_404 = await client.delete("/api/v2/invitations/newmember@test.org")
        assert res_del_404.status_code == 404


async def test_invitations_api_rbac_admin_cannot_invite_owner(monkeypatch, mock_access_verifier):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")

    async def authorize_admin(identity: TenantIdentity) -> TenantContext:
        return identity.context(role="admin")

    test_app = FastAPI()
    test_app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=mock_access_verifier,
        authorize=authorize_admin,
    )
    for route in main_app.routes:
        test_app.routes.append(route)

    headers = {"Cf-Access-Jwt-Assertion": "valid-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="https://origin.test",
    ) as client:
        # Admin tentando convidar Owner -> 403 Forbidden
        res_forbidden = await client.post(
            "/api/v2/invitations",
            json={"email": "other-owner@acme.com", "role": "owner"},
            headers=headers,
        )
        assert res_forbidden.status_code == 403

        # Admin convidando Member -> 201 Created
        res_ok = await client.post(
            "/api/v2/invitations",
            json={"email": "normal-member@acme.com", "role": "member"},
            headers=headers,
        )
        assert res_ok.status_code == 201

