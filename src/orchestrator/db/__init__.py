"""Interface pública da persistência PostgreSQL multi-tenant."""

from orchestrator.db.database import Database, TenantAuthorizationError
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.db.admin import (
    MEMBERSHIP_ROLES,
    RUNTIME_ROLE,
    create_organization,
    grant_membership,
    provision_runtime_role,
    revoke_membership,
)
from orchestrator.db.creators import PostgresCreatorRepository
from orchestrator.db.feedback import PostgresFeedbackRepository
from orchestrator.db.effects import (
    EffectReservation,
    PostgresEffectLedger,
    QuotaExceededError,
    UncertainEffectError,
)
from orchestrator.db.jobs import (
    Job,
    LeaseLostError,
    OutboxEntry,
    PostgresJobRepository,
    RunGate,
    RunEvent,
    StaleGateError,
)
from orchestrator.db.migrations import upgrade_database
from orchestrator.db.prompts import PostgresPromptRepository
from orchestrator.db.runs import PostgresRunRepository, RunIndexEntry, RunSnapshot
from orchestrator.db.tenancy import TenantContext, TenantIdentity

__all__ = [
    "Database",
    "TenantAuthorizationError",
    "PostgresArtifactRepository",
    "RUNTIME_ROLE",
    "MEMBERSHIP_ROLES",
    "PostgresCreatorRepository",
    "PostgresFeedbackRepository",
    "EffectReservation",
    "PostgresEffectLedger",
    "PostgresJobRepository",
    "PostgresPromptRepository",
    "PostgresRunRepository",
    "RunIndexEntry",
    "QuotaExceededError",
    "Job",
    "LeaseLostError",
    "OutboxEntry",
    "RunGate",
    "RunEvent",
    "StaleGateError",
    "RunSnapshot",
    "TenantContext",
    "TenantIdentity",
    "UncertainEffectError",
    "upgrade_database",
    "provision_runtime_role",
    "create_organization",
    "grant_membership",
    "revoke_membership",
]
