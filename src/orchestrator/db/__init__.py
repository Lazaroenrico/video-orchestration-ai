"""Interface pública da persistência PostgreSQL multi-tenant."""

from orchestrator.db.admin import (
    RUNTIME_ROLE,
    create_organization,
    grant_membership,
    owner_bootstrap,
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
from orchestrator.db.invitations import (
    InvitationConflictError,
    InvitationRecord,
    PostgresInvitationRepository,
    normalize_email,
)
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
from orchestrator.db.members import (
    ExistingMemberError,
    LastOwnerError,
    MemberRecord,
    MemberRepository,
    PostgresMemberRepository,
)
from orchestrator.db.migrations import upgrade_database
from orchestrator.db.prompts import PostgresPromptRepository
from orchestrator.db.roles import (
    MEMBERSHIP_ROLES,
    VALID_ROLES,
    validate_role,
)
from orchestrator.db.runs import PostgresRunRepository, RunIndexEntry, RunSnapshot
from orchestrator.db.tenancy import TenantContext, TenantIdentity

__all__ = [
    "ExistingMemberError",
    "LastOwnerError",
    "MemberRecord",
    "MemberRepository",
    "PostgresMemberRepository",
    "InvitationConflictError",
    "InvitationRecord",
    "PostgresInvitationRepository",
    "normalize_email",
    "CancellationSummary",
    "CancelledGateError",
    "Database",
    "TenantAuthorizationError",
    "PostgresArtifactRepository",
    "RUNTIME_ROLE",
    "MEMBERSHIP_ROLES",
    "VALID_ROLES",
    "validate_role",
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
    "owner_bootstrap",
    "get_shared_database",
    "close_shared_database",
]
