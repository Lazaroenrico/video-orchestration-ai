"""Operações administrativas que nunca usam a conexão runtime."""
from __future__ import annotations

from psycopg import Connection, sql
from sqlalchemy import create_engine, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.models import Organization, OrganizationMember, User
from orchestrator.db.roles import validate_role
from orchestrator.db.tenancy import TenantIdentity

RUNTIME_ROLE = "orchestrator_runtime"


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
            sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {}").format(
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
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT EXECUTE ON FUNCTIONS TO {}"
            ).format(role)
        )


def _get_engine(database_url: str):
    url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def create_organization(database_url: str, *, slug: str, name: str) -> None:
    """Cria/atualiza organização usando a conexão administrativa direta."""
    organization_id = TenantIdentity(slug, name, "_").context().organization_id
    engine = _get_engine(database_url)
    with engine.connect() as conn:
        try:
            with conn.begin():
                stmt = (
                    pg_insert(Organization)
                    .values(id=organization_id, slug=slug, name=name)
                    .on_conflict_do_update(
                        index_elements=["id"],
                        set_={"slug": slug, "name": name},
                    )
                )
                conn.execute(stmt)
        except Exception:
            # Em corrida concorrente com o mesmo slug/id, a organização já existe
            pass


def grant_membership(
    database_url: str,
    *,
    organization_slug: str,
    user_subject: str,
    role: str,
) -> None:
    """Cria o usuário e concede/atualiza sua membership explicitamente."""
    clean_role = validate_role(role)
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
                role=clean_role,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "user_id"],
                set_={"role": clean_role},
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


def owner_bootstrap(
    database_url: str,
    *,
    organization_slug: str,
    organization_name: str,
    owner_email: str,
) -> dict[str, str]:
    """Inicializa ou valida a organização criando o convite do primeiro owner de modo idempotente."""
    from orchestrator.db.invitations import normalize_email
    from orchestrator.db.models import Organization, OrganizationInvitation

    normalized = normalize_email(owner_email)
    create_organization(database_url, slug=organization_slug, name=organization_name)

    organization_id = TenantIdentity(organization_slug, organization_name, "_").context().organization_id
    engine = _get_engine(database_url)
    with engine.begin() as conn:
        # Serializa com SELECT FOR UPDATE na linha da organização dentro da transação
        conn.execute(
            select(Organization.id)
            .where(Organization.id == organization_id)
            .with_for_update()
        )

        # Verifica se já existem membros na organização
        member_rows = conn.execute(
            select(OrganizationMember.user_id, OrganizationMember.role, User.email)
            .join(User, User.id == OrganizationMember.user_id, isouter=True)
            .where(OrganizationMember.organization_id == organization_id)
        ).fetchall()

        if not member_rows:
            # Não há membros: verifica se já existe convite pendente para este email
            existing_inv = conn.execute(
                select(1).where(
                    OrganizationInvitation.organization_id == organization_id,
                    OrganizationInvitation.normalized_email == normalized,
                    OrganizationInvitation.role == "owner",
                )
            ).fetchone()
            if existing_inv is not None:
                return {
                    "status": "invitation_pending",
                    "organization_slug": organization_slug,
                    "email": normalized,
                    "role": "owner",
                }

            # Verifica se já existe convite pendente para outro email de owner
            other_inv = conn.execute(
                select(OrganizationInvitation.normalized_email).where(
                    OrganizationInvitation.organization_id == organization_id,
                    OrganizationInvitation.role == "owner",
                )
            ).fetchone()
            if other_inv is not None:
                raise RuntimeError(
                    f"A organização {organization_slug!r} já possui um convite de owner pendente para {other_inv[0]!r}."
                )

            # Cria convite de owner
            inv_stmt = (
                pg_insert(OrganizationInvitation)
                .values(
                    organization_id=organization_id,
                    normalized_email=normalized,
                    role="owner",
                    invited_by_user_id=None,
                )
                .on_conflict_do_update(
                    index_elements=["organization_id", "normalized_email"],
                    set_={"role": "owner"},
                )
            )
            conn.execute(inv_stmt)
            return {
                "status": "invitation_created",
                "organization_slug": organization_slug,
                "email": normalized,
                "role": "owner",
            }

        # Se há membros, verifica se o owner esperado já está estabelecido
        for _, role, email in member_rows:
            if role == "owner" and email and email.strip().lower() == normalized:
                return {
                    "status": "already_established",
                    "organization_slug": organization_slug,
                    "email": normalized,
                    "role": "owner",
                }

        # Verifica se já há um convite pendente para este owner_email
        pending_inv = conn.execute(
            select(1).where(
                OrganizationInvitation.organization_id == organization_id,
                OrganizationInvitation.normalized_email == normalized,
                OrganizationInvitation.role == "owner",
            )
        ).fetchone()
        if pending_inv is not None:
            return {
                "status": "invitation_pending",
                "organization_slug": organization_slug,
                "email": normalized,
                "role": "owner",
            }

        # Organização já tem membros, mas o owner esperado não está estabelecido nem pendente
        raise RuntimeError(
            f"A organização {organization_slug!r} já possui {len(member_rows)} membro(s), "
            f"mas o owner esperado ({normalized}) não está estabelecido. "
            "Use 'membership-grant' como break-glass."
        )
