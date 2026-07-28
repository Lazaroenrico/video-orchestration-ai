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
        attributes = connection.execute(
            """
            SELECT rolsuper, rolbypassrls, rolreplication
            FROM pg_roles
            WHERE rolname = %s
            """,
            (RUNTIME_ROLE,),
        ).fetchone()
        if attributes is None or any(attributes):
            raise ValueError(
                f"papel runtime {RUNTIME_ROLE!r} possui "
                "SUPERUSER/BYPASSRLS/REPLICATION e exige hardening por superuser"
            )
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOCREATEDB NOCREATEROLE PASSWORD {}"
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


from sqlalchemy import create_engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.models import Organization, OrganizationMember, User



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
        attributes = connection.execute(
            """
            SELECT rolsuper, rolbypassrls, rolreplication
            FROM pg_roles
            WHERE rolname = %s
            """,
            (RUNTIME_ROLE,),
        ).fetchone()
        if attributes is None or any(attributes):
            raise ValueError(
                f"papel runtime {RUNTIME_ROLE!r} possui "
                "SUPERUSER/BYPASSRLS/REPLICATION e exige hardening por superuser"
            )
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOCREATEDB NOCREATEROLE PASSWORD {}"
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


def _get_engine(database_url: str):
    url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def create_organization(database_url: str, *, slug: str, name: str) -> None:
    """Cria/atualiza organização usando a conexão administrativa direta."""
    organization_id = TenantIdentity(slug, name, "_").context().organization_id
    engine = _get_engine(database_url)
    with engine.begin() as conn:
        stmt = (
            pg_insert(Organization)
            .values(id=organization_id, slug=slug, name=name)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"slug": slug, "name": name},
            )
        )
        conn.execute(stmt)


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
    engine = _get_engine(database_url)
    with engine.begin() as conn:
        existing = conn.execute(
            select(1).where(
                Organization.id == tenant.organization_id,
                Organization.slug == organization_slug,
            )
        ).fetchone()
        if existing is None:
            raise ValueError(f"organização {organization_slug!r} não existe")

        user_stmt = (
            pg_insert(User)
            .values(id=tenant.user_id, subject=user_subject)
            .on_conflict_do_update(
                index_elements=["id"],
                set_={"subject": user_subject},
            )
        )
        conn.execute(user_stmt)

        member_stmt = (
            pg_insert(OrganizationMember)
            .values(
                organization_id=tenant.organization_id,
                user_id=tenant.user_id,
                role=role,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "user_id"],
                set_={"role": role},
            )
        )
        conn.execute(member_stmt)


def revoke_membership(
    database_url: str,
    *,
    organization_slug: str,
    user_subject: str,
) -> bool:
    """Revoga membership; mantém o usuário para auditoria/reconcessão."""
    tenant = TenantIdentity(organization_slug, organization_slug, user_subject).context()
    engine = _get_engine(database_url)
    with engine.begin() as conn:
        result = conn.execute(
            delete(OrganizationMember).where(
                OrganizationMember.organization_id == tenant.organization_id,
                OrganizationMember.user_id == tenant.user_id,
            )
        )
        return result.rowcount > 0

