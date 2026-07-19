"""Interface pública da persistência PostgreSQL multi-tenant."""

from orchestrator.db.database import Database
from orchestrator.db.migrations import upgrade_database
from orchestrator.db.prompts import PostgresPromptRepository
from orchestrator.db.tenancy import TenantContext, TenantIdentity

__all__ = [
    "Database",
    "PostgresPromptRepository",
    "TenantContext",
    "TenantIdentity",
    "upgrade_database",
]
