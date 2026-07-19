"""Identidade de entrada e contexto resolvido para o tenant atual."""
from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID, NAMESPACE_URL, uuid5


_ID_NAMESPACE = "https://ugc-orchestrator.local/tenancy"
_ENVIRONMENT_FIELDS = {
    "ORCH_ORGANIZATION_SLUG": "organization_slug",
    "ORCH_ORGANIZATION_NAME": "organization_name",
    "ORCH_USER_SUBJECT": "user_subject",
}


def _stable_id(kind: str, value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"{_ID_NAMESPACE}/{kind}/{value}")


@dataclass(frozen=True)
class TenantIdentity:
    organization_slug: str
    organization_name: str
    user_subject: str

    @classmethod
    def from_env(cls) -> "TenantIdentity":
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
