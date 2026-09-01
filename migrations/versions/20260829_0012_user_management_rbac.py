"""Adiciona campos de perfil e políticas RLS granulares para gestão de membros e RBAC."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0012"
down_revision: str | None = "20260804_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORGANIZATION = "NULLIF(current_setting('app.organization_id', true), '')::uuid"
_CURRENT_USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
_MEMBERSHIP_0001 = (
    f"(EXISTS (SELECT 1 FROM organization_members membership "
    f"WHERE membership.user_id = users.id AND membership.organization_id = {_CURRENT_ORGANIZATION}))"
)


def upgrade() -> None:
    # 0. Preflight check: valida que a conexão migradora possui BYPASSRLS ou SUPERUSER
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).fetchone()
    if result is not None:
        is_super, is_bypass = result
        if not (is_super or is_bypass):
            raise RuntimeError(
                "A migração 20260829_0012 exige privilégio de BYPASSRLS ou SUPERUSER para "
                "definir funções SECURITY DEFINER de RLS sem recursão. "
                "Configure MIGRATION_DATABASE_URL com um papel administrativo/migrador privilegiado "
                "(ex.: 'orchestrator' no ambiente local ou admin gerenciado em staging/produção)."
            )

    # 1. Adiciona campos de perfil ao usuário
    op.add_column("users", sa.Column("email", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))

    # 2. Cria funções auxiliares SECURITY DEFINER para quebrar recursão de RLS em organization_members
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.current_actor_role()
        RETURNS text
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = off
        AS $$
          SELECT role
          FROM public.organization_members
          WHERE organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid
            AND user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
          LIMIT 1;
        $$;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.organization_has_members()
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = off
        AS $$
          SELECT EXISTS (
            SELECT 1
            FROM public.organization_members
            WHERE organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid
          );
        $$;
        """
    )

    # 3. Hardening de permissões das funções (remove de PUBLIC e concede aos papéis runtime existentes)
    op.execute("REVOKE EXECUTE ON FUNCTION public.current_actor_role() FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION public.organization_has_members() FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator_runtime') THEN
                GRANT EXECUTE ON FUNCTION public.current_actor_role() TO orchestrator_runtime;
                GRANT EXECUTE ON FUNCTION public.organization_has_members() TO orchestrator_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_app') THEN
                GRANT EXECUTE ON FUNCTION public.current_actor_role() TO tenant_app;
                GRANT EXECUTE ON FUNCTION public.organization_has_members() TO tenant_app;
            END IF;
        END
        $$;
        """
    )

    # 4. Ajusta políticas de users para permitir que admin/owner gerencie usuários (SELECT, INSERT e UPDATE)
    op.execute("DROP POLICY IF EXISTS users_select_by_membership ON users")
    op.execute("DROP POLICY IF EXISTS users_select_rbac ON users")
    op.execute(
        "CREATE POLICY users_select_rbac ON users FOR SELECT "
        f"USING (id = {_CURRENT_USER} OR {_MEMBERSHIP_0001})"
    )

    op.execute("DROP POLICY IF EXISTS users_insert_self ON users")
    op.execute("DROP POLICY IF EXISTS users_insert_rbac ON users")
    op.execute(
        "CREATE POLICY users_insert_rbac ON users FOR INSERT "
        f"WITH CHECK (id = {_CURRENT_USER} OR public.current_actor_role() IN ('owner', 'admin'))"
    )

    op.execute("DROP POLICY IF EXISTS users_update_by_membership ON users")
    op.execute("DROP POLICY IF EXISTS users_update_rbac ON users")
    op.execute(
        "CREATE POLICY users_update_rbac ON users FOR UPDATE "
        f"USING (id = {_CURRENT_USER} OR public.current_actor_role() IN ('owner', 'admin')) "
        f"WITH CHECK (id = {_CURRENT_USER} OR public.current_actor_role() IN ('owner', 'admin'))"
    )

    # 5. Substitui a política genérica de organization_members por políticas granulares por ação e papel
    op.execute("DROP POLICY IF EXISTS organization_members_tenant_isolation ON organization_members")

    op.execute(
        "CREATE POLICY organization_members_select ON organization_members FOR SELECT "
        f"USING (organization_id = {_CURRENT_ORGANIZATION})"
    )

    op.execute(
        "CREATE POLICY organization_members_insert ON organization_members FOR INSERT "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer')) OR "
        f"(public.current_actor_role() IS NULL AND NOT public.organization_has_members() AND role = 'owner' AND user_id = {_CURRENT_USER})"
        "))"
    )

    op.execute(
        "CREATE POLICY organization_members_update ON organization_members FOR UPDATE "
        f"USING (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer'))"
        f")) WITH CHECK (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer'))"
        "))"
    )

    op.execute(
        "CREATE POLICY organization_members_delete ON organization_members FOR DELETE "
        f"USING (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer'))"
        "))"
    )


def downgrade() -> None:
    # Restaura políticas originais da revisão 0001
    op.execute("DROP POLICY IF EXISTS organization_members_delete ON organization_members")
    op.execute("DROP POLICY IF EXISTS organization_members_update ON organization_members")
    op.execute("DROP POLICY IF EXISTS organization_members_insert ON organization_members")
    op.execute("DROP POLICY IF EXISTS organization_members_select ON organization_members")

    op.execute(
        "CREATE POLICY organization_members_tenant_isolation ON organization_members "
        f"USING (organization_id = {_CURRENT_ORGANIZATION}) "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION})"
    )

    op.execute("DROP POLICY IF EXISTS users_update_rbac ON users")
    op.execute(
        "CREATE POLICY users_update_by_membership ON users FOR UPDATE "
        f"USING ({_MEMBERSHIP_0001}) WITH CHECK ({_MEMBERSHIP_0001})"
    )

    op.execute("DROP POLICY IF EXISTS users_insert_rbac ON users")
    op.execute(
        "CREATE POLICY users_insert_self ON users FOR INSERT "
        f"WITH CHECK (id = {_CURRENT_USER})"
    )

    op.execute("DROP POLICY IF EXISTS users_select_rbac ON users")
    op.execute(
        "CREATE POLICY users_select_by_membership ON users FOR SELECT "
        f"USING (id = {_CURRENT_USER} OR {_MEMBERSHIP_0001})"
    )

    op.execute("DROP FUNCTION IF EXISTS public.organization_has_members()")
    op.execute("DROP FUNCTION IF EXISTS public.current_actor_role()")

    op.drop_column("users", "display_name")
    op.drop_column("users", "email")
