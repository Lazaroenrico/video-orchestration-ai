"""Cria identidade, memberships e a barreira RLS inicial da ADR-D36."""
from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CURRENT_ORGANIZATION = (
    "NULLIF(current_setting('app.organization_id', true), '')::uuid"
)
_CURRENT_USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "organization_members",
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="ck_organization_members_role",
        ),
    )

    for table, tenant_column in (
        ("organizations", "id"),
        ("organization_members", "organization_id"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING ({tenant_column} = {_CURRENT_ORGANIZATION}) "
            f"WITH CHECK ({tenant_column} = {_CURRENT_ORGANIZATION})"
        )

    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    membership = (
        "EXISTS (SELECT 1 FROM organization_members AS membership "
        "WHERE membership.user_id = users.id "
        f"AND membership.organization_id = {_CURRENT_ORGANIZATION})"
    )
    op.execute(
        "CREATE POLICY users_select_by_membership ON users FOR SELECT "
        f"USING (id = {_CURRENT_USER} OR {membership})"
    )
    op.execute(
        "CREATE POLICY users_insert_self ON users FOR INSERT "
        f"WITH CHECK (id = {_CURRENT_USER})"
    )
    op.execute(
        "CREATE POLICY users_update_by_membership ON users FOR UPDATE "
        f"USING ({membership}) WITH CHECK ({membership})"
    )
    op.execute(
        "CREATE POLICY users_delete_by_membership ON users FOR DELETE "
        f"USING ({membership})"
    )


def downgrade() -> None:
    op.drop_table("organization_members")
    op.drop_table("users")
    op.drop_table("organizations")
