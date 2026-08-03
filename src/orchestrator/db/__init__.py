"""Interface pública da persistência PostgreSQL multi-tenant."""

from orchestrator.db.admin import (
    MEMBERSHIP_ROLES,
    RUNTIME_ROLE,
    create_organization,
    grant_membership,
    provision_runtime_role,
    revoke_membership,
)
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.db.creators import PostgresCreatorRepository
from orchestrator.db.database import (
    Database,
    TenantAuthorizationError,
    close_shared_database,
    get_shared_database,
)
from orchestrator.db.effects import (
    EffectReservation,
    PostgresEffectLedger,
    QuotaExceededError,
    UncertainEffectError,
)
from orchestrator.db.feedback import PostgresFeedbackRepository
from orchestrator.db.jobs import (
    CancellationSummary,
    CancelledGateError,
    Job,
    LeaseLostError,
    OutboxEntry,
    PostgresJobRepository,
    RunEvent,
    RunGate,
    StaleGateError,
)
from orchestrator.db.migrations import upgrade_database
from orchestrator.db.prompts import PostgresPromptRepository
from orchestrator.db.runs import PostgresRunRepository, RunIndexEntry, RunSnapshot
from orchestrator.db.tenancy import TenantContext, TenantIdentity

__all__ = [
    "CancellationSummary",
    "CancelledGateError",
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
    "get_shared_database",
    "close_shared_database",
]
