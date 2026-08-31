"""Autenticação Cloudflare Access e autorização baseada em papéis (RBAC)."""
from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException
from jwt import PyJWTError
from jwt.algorithms import RSAAlgorithm
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from orchestrator.db.database import Database, TenantAuthorizationError
from orchestrator.db.tenancy import (
    TenantContext,
    TenantIdentity,
    tenant_identity_context,
)


class AccessTokenError(ValueError):
    """Token ausente, inválido ou incompatível com o aplicativo Access."""


class Permission(str, Enum):
    READ = "read"
    RUNS_CREATE = "runs:create"
    RUNS_REVIEW = "runs:review"
    RUNS_RETRY = "runs:retry"
    RUNS_VOICE_REROLL = "runs:voice_reroll"
    PROMPTS_WRITE = "prompts:write"
    MEMBERS_READ = "members:read"
    MEMBERS_WRITE = "members:write"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({Permission.READ}),
    "member": frozenset({
        Permission.READ,
        Permission.RUNS_CREATE,
        Permission.RUNS_REVIEW,
        Permission.RUNS_RETRY,
        Permission.RUNS_VOICE_REROLL,
        Permission.PROMPTS_WRITE,
    }),
    "admin": frozenset({
        Permission.READ,
        Permission.RUNS_CREATE,
        Permission.RUNS_REVIEW,
        Permission.RUNS_RETRY,
        Permission.RUNS_VOICE_REROLL,
        Permission.PROMPTS_WRITE,
        Permission.MEMBERS_READ,
        Permission.MEMBERS_WRITE,
    }),
    "owner": frozenset({
        Permission.READ,
        Permission.RUNS_CREATE,
        Permission.RUNS_REVIEW,
        Permission.RUNS_RETRY,
        Permission.RUNS_VOICE_REROLL,
        Permission.PROMPTS_WRITE,
        Permission.MEMBERS_READ,
        Permission.MEMBERS_WRITE,
    }),
}

_SAFE_CLAIMS = {"email", "name", "display_name", "preferred_username"}


@dataclass(frozen=True)
class RequestPrincipal:
    tenant: TenantContext
    user_id: UUID
    user_subject: str
    organization_id: UUID
    organization_slug: str
    organization_name: str
    role: str
    permissions: frozenset[Permission]
    claims: dict[str, Any]

    @classmethod
    def from_tenant(
        cls,
        tenant: TenantContext,
        organization_name: str,
        claims: Mapping[str, Any] | None = None,
    ) -> RequestPrincipal:
        safe_claims = {
            k: str(v)
            for k, v in (claims or {}).items()
            if k.lower() in _SAFE_CLAIMS and v is not None
        }
        role = tenant.role or "viewer"
        permissions = ROLE_PERMISSIONS.get(role, frozenset({Permission.READ}))
        return cls(
            tenant=tenant,
            user_id=tenant.user_id,
            user_subject=tenant.user_subject,
            organization_id=tenant.organization_id,
            organization_slug=tenant.organization_slug,
            organization_name=organization_name,
            role=role,
            permissions=permissions,
            claims=safe_claims,
        )

    def has_permission(self, permission: Permission | str) -> bool:
        perm = Permission(permission) if isinstance(permission, str) else permission
        return perm in self.permissions

    def can_manage_role(self, target_role: str) -> bool:
        if self.role == "owner":
            return True
        if self.role == "admin":
            return target_role in ("member", "viewer")
        return False


_REQUEST_PRINCIPAL: ContextVar[RequestPrincipal | None] = ContextVar(
    "orchestrator_request_principal", default=None
)


@contextmanager
def request_principal_context(principal: RequestPrincipal) -> Iterator[None]:
    token = _REQUEST_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _REQUEST_PRINCIPAL.reset(token)


def get_current_principal() -> RequestPrincipal:
    principal = _REQUEST_PRINCIPAL.get()
    if principal is not None:
        return principal
    if os.environ.get("ORCH_AUTH_MODE", "disabled") != "cloudflare_access":
        slug = os.environ.get("ORCH_ORGANIZATION_SLUG", "local")
        name = os.environ.get("ORCH_ORGANIZATION_NAME", "Local Organization")
        subject = os.environ.get("ORCH_USER_SUBJECT", "local-user")
        identity = TenantIdentity(organization_slug=slug, organization_name=name, user_subject=subject)
        tenant = identity.context(role="owner")
        return RequestPrincipal.from_tenant(
            tenant,
            organization_name=identity.organization_name,
            claims={"email": f"{subject}@local.test", "name": subject.capitalize()},
        )
    raise HTTPException(status_code=401, detail="Não autenticado")


def require_permission(permission: Permission):
    async def dependency(
        principal: RequestPrincipal = Depends(get_current_principal),
    ) -> RequestPrincipal:
        if not principal.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Permissão negada: {permission.value}",
            )
        return principal

    return dependency


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


Authorize = Callable[..., Awaitable[TenantContext]]


async def _authorize_with_app_database(
    scope: Scope,
    identity: TenantIdentity,
    verified_email: str | None = None,
) -> TenantContext:
    import inspect

    app = scope["app"]
    database = getattr(app.state, "auth_database", None)
    if database is None:
        async with Database.from_env() as transient_database:
            sig = inspect.signature(transient_database.authorize_tenant)
            if len(sig.parameters) >= 2 or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                return await transient_database.authorize_tenant(
                    identity, verified_email=verified_email
                )
            return await transient_database.authorize_tenant(identity)

    sig = inspect.signature(database.authorize_tenant)
    if len(sig.parameters) >= 2 or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    ):
        return await database.authorize_tenant(identity, verified_email=verified_email)
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
        organization_slug = os.environ.get("ORCH_ORGANIZATION_SLUG", "").strip()
        organization_name = os.environ.get("ORCH_ORGANIZATION_NAME", "").strip()
        if not organization_slug or not organization_name:
            await self._reject(scope, receive, send, 503, "configuração de tenant ausente no servidor")
            return

        try:
            verifier = self.verifier or AccessJwtVerifier.from_env()
            claims = await verifier.verify(token)
            subject = str(claims.get("sub", "")).strip()
            if not subject:
                raise AccessTokenError("token Access sem subject")
            raw_email = claims.get("email")
            verified_email = str(raw_email).strip() if raw_email else None
            identity = TenantIdentity(organization_slug, organization_name, subject)
            if self.authorize is not None:
                import inspect

                sig = inspect.signature(self.authorize)
                if len(sig.parameters) >= 2 or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    tenant = await self.authorize(identity, verified_email)
                else:
                    tenant = await self.authorize(identity)
            else:
                import inspect

                sig = inspect.signature(_authorize_with_app_database)
                if len(sig.parameters) >= 3 or "verified_email" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    tenant = await _authorize_with_app_database(
                        scope, identity, verified_email=verified_email
                    )
                else:
                    tenant = await _authorize_with_app_database(scope, identity)
        except AccessTokenError as exc:
            await self._reject(scope, receive, send, 401, str(exc))
            return
        except TenantAuthorizationError as exc:
            await self._reject(scope, receive, send, 403, str(exc))
            return
        except (ValueError, httpx.HTTPError) as exc:
            await self._reject(scope, receive, send, 503, str(exc))
            return

        principal = RequestPrincipal.from_tenant(
            tenant,
            organization_name=organization_name,
            claims=claims,
        )

        with tenant_identity_context(identity), request_principal_context(principal):
            await self.app(scope, receive, send)
