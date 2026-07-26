"""Operações administrativas que nunca usam a conexão runtime."""
from __future__ import annotations

from psycopg import Connection, sql

from orchestrator.db.tenancy import TenantIdentity


RUNTIME_ROLE = "orchestrator_runtime"
MEMBERSHIP_ROLES = ("owner", "admin", "member", "viewer")


def provision_runtime_role(database_url: str, password: str) -> None:
    """Cria ou endurece o papel fixo usado pela API e pelos runners."""
    if not password:
        raise ValueError("ORCHESTRATOR_RUNTIME_PASSWORD é obrigatória")

    with Connection.connect(database_url) as connection:
        role = sql.Identifier(RUNTIME_ROLE)
        connection.execute(
            sql.SQL(
                "DO $$ BEGIN CREATE ROLE {} LOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
            ).format(role, sql.Literal(password)),
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(connection.info.dbname),
                role,
            )
        )
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON ALL TABLES IN SCHEMA public TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(role)
        )


def create_organization(database_url: str, *, slug: str, name: str) -> None:
    """Cria/atualiza organização usando a conexão administrativa direta."""
    organization_id = TenantIdentity(slug, name, "_").context().organization_id
    with Connection.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO organizations (id, slug, name)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET slug = EXCLUDED.slug, name = EXCLUDED.name
            """,
            (organization_id, slug, name),
        )


def grant_membership(
    database_url: str,
    *,
    organization_slug: str,
    user_subject: str,
    role: str,
) -> None:
    """Cria o usuário e concede/atualiza sua membership explicitamente."""
    if role not in MEMBERSHIP_ROLES:
        raise ValueError(f"papel de membership inválido: {role!r}")
    tenant = TenantIdentity(organization_slug, organization_slug, user_subject).context()
    with Connection.connect(database_url) as connection:
        existing = connection.execute(
            "SELECT 1 FROM organizations WHERE id = %s AND slug = %s",
            (tenant.organization_id, organization_slug),
        ).fetchone()
        if existing is None:
            raise ValueError(f"organização {organization_slug!r} não existe")
        connection.execute(
            """
            INSERT INTO users (id, subject)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET subject = EXCLUDED.subject
            """,
            (tenant.user_id, user_subject),
        )
        connection.execute(
            """
            INSERT INTO organization_members (organization_id, user_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (organization_id, user_id)
            DO UPDATE SET role = EXCLUDED.role
            """,
            (tenant.organization_id, tenant.user_id, role),
        )


def revoke_membership(
    database_url: str,
    *,
    organization_slug: str,
    user_subject: str,
) -> bool:
    """Revoga membership; mantém o usuário para auditoria/reconcessão."""
    tenant = TenantIdentity(organization_slug, organization_slug, user_subject).context()
    with Connection.connect(database_url) as connection:
        cursor = connection.execute(
            """
            DELETE FROM organization_members
            WHERE organization_id = %s AND user_id = %s
            """,
            (tenant.organization_id, tenant.user_id),
        )
        return cursor.rowcount > 0
