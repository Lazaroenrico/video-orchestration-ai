"""Identidade de entrada e contexto resolvido para o tenant atual."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
from uuid import NAMESPACE_URL, UUID, uuid5

_ID_NAMESPACE = "https://ugc-orchestrator.local/tenancy"
_ENVIRONMENT_FIELDS = {
    "ORCH_ORGANIZATION_SLUG": "organization_slug",
    "ORCH_ORGANIZATION_NAME": "organization_name",
    "ORCH_USER_SUBJECT": "user_subject",
}
_REQUEST_IDENTITY: ContextVar[TenantIdentity | None]


def _stable_id(kind: str, value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{_ID_NAMESPACE}/{kind}/{value}")


@dataclass(frozen=True)
class TenantIdentity:
    organization_slug: str
    organization_name: str
    user_subject: str

    @classmethod
    def from_env(cls) -> "TenantIdentity":
        request_identity = _REQUEST_IDENTITY.get()
        if request_identity is not None:
            return request_identity
        missing = [name for name in _ENVIRONMENT_FIELDS if not os.environ.get(name)]
        if missing:
            raise ValueError(
                "contexto de tenant incompleto; variável ausente: " + ", ".join(missing)
            )
        values = {field: os.environ[name] for name, field in _ENVIRONMENT_FIELDS.items()}
        return cls(**values)

    def context(self) -> "TenantContext":
        return TenantContext(
            organization_id=_stable_id("organization", self.organization_slug),
            user_id=_stable_id("user", self.user_subject),
            organization_slug=self.organization_slug,
            user_subject=self.user_subject,
        )


@dataclass(frozen=True)
class TenantContext:
    organization_id: UUID
    user_id: UUID
    organization_slug: str
    user_subject: str


_REQUEST_IDENTITY = ContextVar("orchestrator_request_tenant_identity", default=None)


@contextmanager
def tenant_identity_context(identity: TenantIdentity) -> Iterator[None]:
    """Vincula identidade ao request/task atual sem contaminar outras coroutines."""
    token = _REQUEST_IDENTITY.set(identity)
    try:
        yield
    finally:
        _REQUEST_IDENTITY.reset(token)
