"""Interface pública da persistência PostgreSQL multi-tenant."""

from orchestrator.db.database import Database
from orchestrator.db.artifacts import PostgresArtifactRepository
from orchestrator.db.admin import RUNTIME_ROLE, provision_runtime_role
from orchestrator.db.creators import PostgresCreatorRepository
from orchestrator.db.feedback import PostgresFeedbackRepository
from orchestrator.db.migrations import upgrade_database
from orchestrator.db.prompts import PostgresPromptRepository
from orchestrator.db.runs import PostgresRunRepository, RunIndexEntry, RunSnapshot
from orchestrator.db.tenancy import TenantContext, TenantIdentity

__all__ = [
    "Database",
    "PostgresArtifactRepository",
    "RUNTIME_ROLE",
    "PostgresCreatorRepository",
    "PostgresFeedbackRepository",
    "PostgresPromptRepository",
    "PostgresRunRepository",
    "RunIndexEntry",
    "RunSnapshot",
    "TenantContext",
    "TenantIdentity",
    "upgrade_database",
    "provision_runtime_role",
]
