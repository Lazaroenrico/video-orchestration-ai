"""Cria tabela organization_invitations, função atômica claim_organization_invitation e políticas RLS."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0013"
down_revision: str | None = "20260829_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT_ORGANIZATION = "NULLIF(current_setting('app.organization_id', true), '')::uuid"


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
                "A migração 20260831_0013 exige privilégio de BYPASSRLS ou SUPERUSER para "
                "definir funções SECURITY DEFINER de claim e políticas RLS de convites. "
                "Configure MIGRATION_DATABASE_URL com um papel administrativo/migrador privilegiado."
            )

    # 1. Cria a tabela organization_invitations
    op.create_table(
        "organization_invitations",
        sa.Column("organization_id", sa.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("normalized_email", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("invited_by_user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.PrimaryKeyConstraint("organization_id", "normalized_email", name="pk_organization_invitations"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member', 'viewer')", name="ck_organization_invitations_role"),
    )
    op.create_index(
        "ix_organization_invitations_org_created",
        "organization_invitations",
        ["organization_id", "created_at"],
    )

    # 2. Habilita e força RLS
    op.execute("ALTER TABLE organization_invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_invitations FORCE ROW LEVEL SECURITY")

    # 3. Cria a função atômica SECURITY DEFINER para claim de convites
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.claim_organization_invitation(
            p_organization_id uuid,
            p_user_id uuid,
            p_user_subject text,
            p_email text,
            p_display_name text DEFAULT NULL
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET row_security = off
        AS $$
        DECLARE
            v_ctx_org_id uuid;
            v_ctx_user_id uuid;
            v_normalized_email text;
            v_role text;
        BEGIN
            -- Validação estrita e fail-closed de igualdade com o contexto da sessão ativa
            v_ctx_org_id := NULLIF(current_setting('app.organization_id', true), '')::uuid;
            v_ctx_user_id := NULLIF(current_setting('app.user_id', true), '')::uuid;

            IF p_organization_id IS NULL OR v_ctx_org_id IS NULL OR p_organization_id != v_ctx_org_id THEN
                RAISE EXCEPTION 'claim_organization_invitation: organization_id mismatch with session context';
            END IF;

            IF p_user_id IS NULL OR v_ctx_user_id IS NULL OR p_user_id != v_ctx_user_id THEN
                RAISE EXCEPTION 'claim_organization_invitation: user_id mismatch with session context';
            END IF;

            v_normalized_email := lower(trim(p_email));
            IF v_normalized_email IS NULL OR v_normalized_email = '' THEN
                RETURN NULL;
            END IF;

            -- Localiza e bloqueia o convite para claim exclusivo
            SELECT role INTO v_role
            FROM public.organization_invitations
            WHERE organization_id = p_organization_id
              AND normalized_email = v_normalized_email
            FOR UPDATE;

            IF v_role IS NULL THEN
                RETURN NULL;
            END IF;

            -- Materializa/atualiza o usuário
            INSERT INTO public.users (id, subject, email, display_name, created_at)
            VALUES (p_user_id, p_user_subject, v_normalized_email, p_display_name, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE
            SET email = EXCLUDED.email,
                display_name = COALESCE(EXCLUDED.display_name, public.users.display_name);

            -- Cria a membership com o papel concedido no convite
            INSERT INTO public.organization_members (organization_id, user_id, role, created_at)
            VALUES (p_organization_id, p_user_id, v_role, CURRENT_TIMESTAMP)
            ON CONFLICT (organization_id, user_id) DO UPDATE
            SET role = EXCLUDED.role;

            -- Remove o convite consumido
            DELETE FROM public.organization_invitations
            WHERE organization_id = p_organization_id
              AND normalized_email = v_normalized_email;

            RETURN v_role;
        END;
        $$;
        """
    )

    # 4. Hardening de permissões da função e concessões à tabela
    op.execute("REVOKE EXECUTE ON FUNCTION public.claim_organization_invitation(uuid, uuid, text, text, text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator_runtime') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON organization_invitations TO orchestrator_runtime;
                GRANT EXECUTE ON FUNCTION public.claim_organization_invitation(uuid, uuid, text, text, text) TO orchestrator_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON organization_invitations TO tenant_app;
                GRANT EXECUTE ON FUNCTION public.claim_organization_invitation(uuid, uuid, text, text, text) TO tenant_app;
            END IF;
        END
        $$;
        """
    )

    # 5. Políticas RLS
    op.execute(
        "CREATE POLICY organization_invitations_select ON organization_invitations FOR SELECT "
        f"USING (organization_id = {_CURRENT_ORGANIZATION} AND "
        "public.current_actor_role() IN ('owner', 'admin'))"
    )

    op.execute(
        "CREATE POLICY organization_invitations_insert ON organization_invitations FOR INSERT "
        f"WITH CHECK (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer')) OR "
        "(public.current_actor_role() IS NULL AND NOT public.organization_has_members() AND role = 'owner')"
        "))"
    )

    op.execute(
        "CREATE POLICY organization_invitations_delete ON organization_invitations FOR DELETE "
        f"USING (organization_id = {_CURRENT_ORGANIZATION} AND ("
        "public.current_actor_role() = 'owner' OR "
        "(public.current_actor_role() = 'admin' AND role IN ('member', 'viewer'))"
        "))"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS organization_invitations_delete ON organization_invitations")
    op.execute("DROP POLICY IF EXISTS organization_invitations_insert ON organization_invitations")
    op.execute("DROP POLICY IF EXISTS organization_invitations_select ON organization_invitations")

    op.execute("DROP FUNCTION IF EXISTS public.claim_organization_invitation(uuid, uuid, text, text, text)")
    op.drop_table("organization_invitations")
