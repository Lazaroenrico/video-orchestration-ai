"""Fronteira Cloudflare Access: JWT identifica e PostgreSQL autoriza."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from orchestrator import auth as auth_module
from orchestrator.auth import (
    AccessJwtVerifier,
    AccessTokenError,
    CloudflareAccessMiddleware,
)
from orchestrator.db import TenantAuthorizationError, TenantContext, TenantIdentity
from orchestrator.db.tenancy import tenant_identity_context


@pytest.fixture
def access_keypair() -> tuple[str, dict[str, str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    numbers = public_key.public_numbers()

    def encoded(number: int) -> str:
        import base64

        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return private_pem, {
        "kty": "RSA",
        "kid": "access-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": encoded(numbers.n),
        "e": encoded(numbers.e),
    }


def _access_token(private_pem: str, *, audience: str = "app-aud") -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "access|alice",
            "aud": audience,
            "iss": "https://team.cloudflareaccess.com",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "access-key-1"},
    )


async def test_access_verifier_validates_signature_issuer_and_audience(access_keypair):
    private_pem, public_jwk = access_keypair
    verifier = AccessJwtVerifier(
        team_domain="https://team.cloudflareaccess.com",
        audience="app-aud",
        jwks={"keys": [public_jwk]},
    )

    claims = await verifier.verify(_access_token(private_pem))

    assert claims["sub"] == "access|alice"


async def test_access_verifier_rejects_another_application_audience(access_keypair):
    private_pem, public_jwk = access_keypair
    verifier = AccessJwtVerifier(
        team_domain="team.cloudflareaccess.com",
        audience="app-aud",
        jwks={"keys": [public_jwk]},
    )

    with pytest.raises(AccessTokenError, match="inválido"):
        await verifier.verify(_access_token(private_pem, audience="other-app"))


async def test_access_middleware_bypasses_health_and_rejects_missing_jwt(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    downstream_calls: list[str] = []

    async def authorize(_identity: TenantIdentity) -> TenantContext:
        raise AssertionError("membership não deve ser consultada sem JWT")

    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=object(),
        authorize=authorize,
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        downstream_calls.append("health")
        return {"status": "ok"}

    @app.get("/api/runs")
    async def runs() -> dict[str, list[str]]:
        downstream_calls.append("api")
        return {"runs": []}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        health_response = await client.get("/healthz")
        api_response = await client.get("/api/runs")

    assert health_response.status_code == 200
    assert api_response.status_code == 401
    assert downstream_calls == ["health"]


async def test_access_middleware_binds_verified_identity_and_authorizes_membership(
    monkeypatch,
):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    authorized: list[TenantIdentity] = []

    class Verifier:
        async def verify(self, token: str) -> dict[str, str]:
            assert token == "signed-access-token"
            return {"sub": "access|alice"}

    async def authorize(identity: TenantIdentity) -> TenantContext:
        authorized.append(identity)
        return identity.context()

    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=authorize,
    )

    @app.get("/api/whoami")
    async def whoami() -> dict[str, str]:
        identity = TenantIdentity.from_env()
        return {
            "organization": identity.organization_slug,
            "subject": identity.user_subject,
        }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/whoami",
            headers={
                "Cf-Access-Jwt-Assertion": "signed-access-token",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "organization": "acme",
        "subject": "access|alice",
    }
    assert authorized == [TenantIdentity("acme", "Acme Inc.", "access|alice")]


async def test_access_middleware_ignores_forged_organization_headers_and_uses_server_config(
    monkeypatch,
):
    """Headers forjados enviados pelo cliente nunca alteram o tenant fixo do servidor."""
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "server-org")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Server Org")
    authorized: list[TenantIdentity] = []

    class Verifier:
        async def verify(self, token: str) -> dict[str, str]:
            return {"sub": "access|alice"}

    async def authorize(identity: TenantIdentity) -> TenantContext:
        authorized.append(identity)
        return identity.context()

    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=authorize,
    )

    @app.get("/api/tenant-check")
    async def tenant_check() -> dict[str, str]:
        identity = TenantIdentity.from_env()
        return {"slug": identity.organization_slug, "name": identity.organization_name}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/tenant-check",
            headers={
                "Cf-Access-Jwt-Assertion": "valid-token",
                "X-Orch-Organization-Slug": "evil-attacker",
                "X-Orch-Organization-Name": "Attacker Inc.",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"slug": "server-org", "name": "Server Org"}
    assert authorized == [TenantIdentity("server-org", "Server Org", "access|alice")]


async def test_access_middleware_fails_safely_when_server_organization_config_is_missing(
    monkeypatch,
):
    """Se a configuração de tenant do deployment estiver ausente em cloudflare_access, falha com 503."""
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.delenv("ORCH_ORGANIZATION_SLUG", raising=False)
    monkeypatch.delenv("ORCH_ORGANIZATION_NAME", raising=False)

    class Verifier:
        async def verify(self, token: str) -> dict[str, str]:
            return {"sub": "access|alice"}

    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=lambda identity: identity.context(),
    )

    @app.get("/api/protected")
    async def protected():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/protected",
            headers={"Cf-Access-Jwt-Assertion": "valid-token"},
        )

    assert response.status_code == 503
    assert "configuração de tenant ausente" in response.json()["detail"].lower()


async def test_request_tenant_context_is_isolated_between_coroutines(monkeypatch):
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "environment")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Environment")
    monkeypatch.setenv("ORCH_USER_SUBJECT", "env|user")

    async def read(identity: TenantIdentity) -> TenantIdentity:
        with tenant_identity_context(identity):
            await asyncio.sleep(0)
            return TenantIdentity.from_env()

    acme, globex = await asyncio.gather(
        read(TenantIdentity("acme", "Acme", "access|alice")),
        read(TenantIdentity("globex", "Globex", "access|bob")),
    )

    assert acme.organization_slug == "acme"
    assert globex.organization_slug == "globex"
    assert TenantIdentity.from_env().organization_slug == "environment"


def test_access_verifier_validates_required_configuration(monkeypatch):
    with pytest.raises(ValueError, match="CF_ACCESS_TEAM_DOMAIN"):
        AccessJwtVerifier(team_domain="", audience="aud")
    with pytest.raises(ValueError, match="HTTPS"):
        AccessJwtVerifier(team_domain="http://access.example.com", audience="aud")
    with pytest.raises(ValueError, match="CF_ACCESS_AUDIENCE"):
        AccessJwtVerifier(team_domain="access.example.com", audience="")

    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com/")
    monkeypatch.setenv("CF_ACCESS_AUDIENCE", "app-aud")
    monkeypatch.setenv("CF_ACCESS_JWKS_JSON", '{"keys": []}')
    verifier = AccessJwtVerifier.from_env()

    assert verifier.team_domain == "https://team.cloudflareaccess.com"
    assert verifier._jwks == {"keys": []}


async def test_access_verifier_fetches_and_rejects_malformed_jwks(
    monkeypatch,
    access_keypair,
):
    _private_pem, public_jwk = access_keypair
    payloads = [{"keys": [public_jwk]}, {"unexpected": []}]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, timeout):
            assert timeout == 10.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def get(self, url):
            assert url == "https://team.cloudflareaccess.com/cdn-cgi/access/certs"
            return Response(payloads.pop(0))

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", Client)
    verifier = AccessJwtVerifier(
        team_domain="team.cloudflareaccess.com",
        audience="app-aud",
    )

    assert await verifier._load_jwks() == {"keys": [public_jwk]}
    with pytest.raises(AccessTokenError, match="JWKS inválido"):
        await verifier._load_jwks(refresh=True)


async def test_access_verifier_rejects_unknown_key_and_non_rs256_token(
    monkeypatch,
):
    verifier = AccessJwtVerifier(
        team_domain="team.cloudflareaccess.com",
        audience="app-aud",
        jwks={"keys": []},
    )

    async def empty_jwks(*, refresh=False):
        assert isinstance(refresh, bool)
        return {"keys": []}

    monkeypatch.setattr(verifier, "_load_jwks", empty_jwks)
    with pytest.raises(AccessTokenError, match="não encontrada"):
        await verifier._key_for("missing")

    hs_token = jwt.encode(
        {"sub": "alice"},
        "not-an-access-key-but-long-enough-32",
        algorithm="HS256",
        headers={"kid": "wrong-algorithm"},
    )
    with pytest.raises(AccessTokenError, match="algoritmo"):
        await verifier.verify(hs_token)


async def test_access_database_authorizer_uses_pool_or_transient_database(monkeypatch):
    identity = TenantIdentity("acme", "Acme", "access|alice")
    calls: list[str] = []

    class FakeDatabase:
        async def __aenter__(self):
            calls.append("enter")
            return self

        async def __aexit__(self, *_exc):
            calls.append("exit")

        async def authorize_tenant(self, candidate):
            assert candidate == identity
            calls.append("authorize")
            return candidate.context()

    transient = FakeDatabase()
    monkeypatch.setattr(
        auth_module.Database,
        "from_env",
        classmethod(lambda cls: transient),
    )
    transient_app = FastAPI()
    assert await auth_module._authorize_with_app_database(
        {"app": transient_app},
        identity,
    ) == identity.context()

    pooled = FakeDatabase()
    pooled_app = FastAPI()
    pooled_app.state.auth_database = pooled
    assert await auth_module._authorize_with_app_database(
        {"app": pooled_app},
        identity,
    ) == identity.context()
    assert calls == ["enter", "authorize", "exit", "authorize"]


async def test_access_middleware_rejects_empty_subject(
    monkeypatch,
):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=type("Verifier", (), {"verify": lambda self, token: _claims("")})(),
    )

    @app.get("/api/protected")
    async def protected():
        return {"unexpected": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/protected",
            headers={"Cf-Access-Jwt-Assertion": "token"},
        )

    assert response.status_code == 401


async def _claims(subject: str) -> dict[str, str]:
    return {"sub": subject}


async def test_access_middleware_maps_membership_and_configuration_errors(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    headers = {
        "Cf-Access-Jwt-Assertion": "token",
    }

    class Verifier:
        async def verify(self, _token):
            return {"sub": "access|alice"}

    async def forbidden(_identity):
        raise TenantAuthorizationError("membership ausente")

    forbidden_app = FastAPI()
    forbidden_app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=forbidden,
    )

    unavailable_app = FastAPI()
    unavailable_app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=None,
        authorize=forbidden,
    )
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUDIENCE", raising=False)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=forbidden_app),
        base_url="https://origin.test",
    ) as client:
        forbidden_response = await client.get("/api/protected", headers=headers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unavailable_app),
        base_url="https://origin.test",
    ) as client:
        unavailable_response = await client.get("/api/protected", headers=headers)

    assert forbidden_response.status_code == 403
    assert unavailable_response.status_code == 503


async def test_access_middleware_uses_the_application_database_by_default(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    authorized: list[TenantIdentity] = []

    class Verifier:
        async def verify(self, _token):
            return {"sub": "access|alice"}

    async def authorize_from_app(_scope, identity):
        authorized.append(identity)
        return identity.context()

    monkeypatch.setattr(
        auth_module,
        "_authorize_with_app_database",
        authorize_from_app,
    )
    app = FastAPI()
    app.add_middleware(CloudflareAccessMiddleware, verifier=Verifier())

    @app.get("/api/protected")
    async def protected():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/protected",
            headers={"Cf-Access-Jwt-Assertion": "token"},
        )

    assert response.status_code == 200
    assert authorized == [TenantIdentity("acme", "Acme Inc.", "access|alice")]


async def test_access_middleware_passes_verified_email_to_authorizer(monkeypatch):
    monkeypatch.setenv("ORCH_AUTH_MODE", "cloudflare_access")
    monkeypatch.setenv("ORCH_ORGANIZATION_SLUG", "acme")
    monkeypatch.setenv("ORCH_ORGANIZATION_NAME", "Acme Inc.")
    received_calls: list[tuple[TenantIdentity, str | None]] = []

    class Verifier:
        async def verify(self, _token):
            return {"sub": "access|bob", "email": "bob@acme.com"}

    async def custom_authorizer(identity: TenantIdentity, verified_email: str | None = None):
        received_calls.append((identity, verified_email))
        return identity.context(role="member")

    app = FastAPI()
    app.add_middleware(
        CloudflareAccessMiddleware,
        verifier=Verifier(),
        authorize=custom_authorizer,
    )

    @app.get("/api/protected")
    async def protected():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://origin.test",
    ) as client:
        response = await client.get(
            "/api/protected",
            headers={"Cf-Access-Jwt-Assertion": "token"},
        )

    assert response.status_code == 200
    assert received_calls == [
        (TenantIdentity("acme", "Acme Inc.", "access|bob"), "bob@acme.com")
    ]
