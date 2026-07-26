"""Autenticação Cloudflare Access sem misturar identidade e autorização."""
from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from jwt import PyJWTError
from jwt.algorithms import RSAAlgorithm
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orchestrator.db.database import Database, TenantAuthorizationError
from orchestrator.db.tenancy import (
    TenantContext,
    TenantIdentity,
    tenant_identity_context,
)


class AccessTokenError(ValueError):
    """Token ausente, inválido ou incompatível com o aplicativo Access."""


def _normalized_team_domain(value: str) -> str:
    candidate = value.strip().rstrip("/")
    if not candidate:
        raise ValueError("CF_ACCESS_TEAM_DOMAIN é obrigatória")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("CF_ACCESS_TEAM_DOMAIN deve ser um domínio HTTPS válido")
    return candidate


class AccessJwtVerifier:
    """Valida tokens RS256 contra o JWKS oficial do domínio Access."""

    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        jwks: Mapping[str, Any] | None = None,
    ) -> None:
        if not audience:
            raise ValueError("CF_ACCESS_AUDIENCE é obrigatória")
        self.team_domain = _normalized_team_domain(team_domain)
        self.audience = audience
        self._jwks = dict(jwks) if jwks is not None else None

    @classmethod
    def from_env(cls) -> "AccessJwtVerifier":
        static_jwks = os.environ.get("CF_ACCESS_JWKS_JSON")
        return cls(
            team_domain=os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""),
            audience=os.environ.get("CF_ACCESS_AUDIENCE", ""),
            jwks=json.loads(static_jwks) if static_jwks else None,
        )

    async def _load_jwks(self, *, refresh: bool = False) -> Mapping[str, Any]:
        if self._jwks is not None and not refresh:
            return self._jwks
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.team_domain}/cdn-cgi/access/certs")
            response.raise_for_status()
            loaded = response.json()
        if not isinstance(loaded, dict) or not isinstance(loaded.get("keys"), list):
            raise AccessTokenError("JWKS inválido devolvido pelo Cloudflare Access")
        self._jwks = loaded
        return loaded

    async def _key_for(self, key_id: str) -> Any:
        for refresh in (False, True):
            jwks = await self._load_jwks(refresh=refresh)
            for key in jwks.get("keys", []):
                if isinstance(key, dict) and key.get("kid") == key_id:
                    return RSAAlgorithm.from_jwk(key)
        raise AccessTokenError("chave de assinatura do Access não encontrada")

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise AccessTokenError("algoritmo ou key id do token Access inválido")
            key = await self._key_for(str(header["kid"]))
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.team_domain,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except AccessTokenError:
            raise
        except (PyJWTError, ValueError, httpx.HTTPError) as exc:
            raise AccessTokenError("token Cloudflare Access inválido") from exc
        return dict(claims)


Authorize = Callable[[TenantIdentity], Awaitable[TenantContext]]


async def _authorize_with_app_database(
    scope: Scope,
    identity: TenantIdentity,
) -> TenantContext:
    app = scope["app"]
    database = getattr(app.state, "auth_database", None)
    if database is None:
        async with Database.from_env() as transient_database:
            return await transient_database.authorize_tenant(identity)
    return await database.authorize_tenant(identity)


class CloudflareAccessMiddleware:
    """Protege `/api/*` sem bufferizar SSE; health/readiness ficam públicos."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: Any | None = None,
        authorize: Authorize | None = None,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.authorize = authorize

    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse({"detail": detail}, status_code=status_code)(
            scope,
            receive,
            send,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or os.environ.get("ORCH_AUTH_MODE", "disabled") != "cloudflare_access"
            or not str(scope.get("path", "")).startswith("/api/")
        ):
            await self.app(scope, receive, send)
            return

        headers = self._headers(scope)
        token = headers.get("cf-access-jwt-assertion")
        if not token:
            await self._reject(scope, receive, send, 401, "JWT do Cloudflare Access ausente")
            return
        organization_slug = headers.get("x-orch-organization-slug", "").strip()
        organization_name = headers.get("x-orch-organization-name", "").strip()
        if not organization_slug or not organization_name:
            await self._reject(scope, receive, send, 400, "tenant da requisição ausente")
            return

        try:
            verifier = self.verifier or AccessJwtVerifier.from_env()
            claims = await verifier.verify(token)
            subject = str(claims.get("sub", "")).strip()
            if not subject:
                raise AccessTokenError("token Access sem subject")
            identity = TenantIdentity(organization_slug, organization_name, subject)
            if self.authorize is not None:
                await self.authorize(identity)
            else:
                await _authorize_with_app_database(scope, identity)
        except AccessTokenError as exc:
            await self._reject(scope, receive, send, 401, str(exc))
            return
        except TenantAuthorizationError as exc:
            await self._reject(scope, receive, send, 403, str(exc))
            return
        except (ValueError, httpx.HTTPError) as exc:
            await self._reject(scope, receive, send, 503, str(exc))
            return

        with tenant_identity_context(identity):
            await self.app(scope, receive, send)
