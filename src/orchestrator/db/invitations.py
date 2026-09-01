"""Módulo profundo de repositório e ciclo de vida de convites (organization_invitations)."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator, Optional, Protocol
from uuid import UUID

from orchestrator.db.database import Database, get_shared_database
from orchestrator.db.roles import validate_role
from orchestrator.db.tenancy import TenantContext, TenantIdentity

if TYPE_CHECKING:
    from orchestrator.auth import RequestPrincipal


class InvitationConflictError(ValueError):
    """Convite conflitante: já existe um convite pendente ou o usuário já é membro."""


def normalize_email(email: str) -> str:
    """Normaliza e-mail com trim e lowercase, validando formato básico não-vazio."""
    if not isinstance(email, str):
        raise ValueError("e-mail inválido")
    candidate = email.strip().lower()
    if not candidate or "@" not in candidate:
        raise ValueError("e-mail inválido")
    local_part, _, domain = candidate.partition("@")
    if not local_part or not domain:
        raise ValueError("e-mail inválido")
    return candidate


@dataclass(frozen=True)
class InvitationRecord:
    organization_id: UUID
    normalized_email: str
    role: str
    invited_by_user_id: Optional[UUID]
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": str(self.organization_id),
            "email": self.normalized_email,
            "role": self.role,
            "invited_by_user_id": str(self.invited_by_user_id) if self.invited_by_user_id else None,
            "created_at": self.created_at.isoformat(),
        }


class InvitationRepository(Protocol):
    async def list_invitations(self) -> list[InvitationRecord]: ...
    async def create_invitation(
        self,
        email: str,
        role: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> InvitationRecord: ...
    async def cancel_invitation(
        self,
        email: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> bool: ...
    async def claim_invitation(
        self,
        user_id: UUID,
        user_subject: str,
        email: str,
        display_name: Optional[str] = None,
    ) -> Optional[str]: ...


class PostgresInvitationRepository:
    """Repositório transacional de convites para a organização do tenant."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self.database = database
        self.tenant = tenant

    async def list_invitations(self) -> list[InvitationRecord]:
        """Lista convites pendentes da organização em ordem decrescente de criação."""
        async with self.database.connection(self.tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT organization_id, normalized_email, role, invited_by_user_id, created_at
                FROM organization_invitations
                WHERE organization_id = %s
                ORDER BY created_at DESC
                """,
                (self.tenant.organization_id,),
            )
            rows = await cursor.fetchall()
            return [
                InvitationRecord(
                    organization_id=row[0],
                    normalized_email=str(row[1]),
                    role=str(row[2]),
                    invited_by_user_id=row[3],
                    created_at=row[4],
                )
                for row in rows
            ]

    async def create_invitation(
        self,
        email: str,
        role: str,
        actor_principal: Optional["RequestPrincipal"] = None,
    ) -> InvitationRecord:
        """Cria um novo convite validando papel, RBAC e conflitos com membros/convites."""
        normalized = normalize_email(email)
        clean_role = validate_role(role)

        if actor_principal is not None:
            if not actor_principal.can_manage_role(clean_role):
                raise PermissionError(
                    f"papel {actor_principal.role} não tem permissão para gerenciar o papel {clean_role}"
                )

        invited_by = actor_principal.user_id if actor_principal else None

        try:
            async with self.database.connection(self.tenant) as connection:
                # 1. Verifica se já existe membro ativo com este e-mail
                cursor = await connection.execute(
                    """
                    SELECT 1
                    FROM organization_members AS m
                    JOIN users AS u ON u.id = m.user_id
                    WHERE m.organization_id = %s
                      AND lower(trim(u.email)) = %s
                    LIMIT 1
                    """,
                    (self.tenant.organization_id, normalized),
                )
                if await cursor.fetchone() is not None:
                    raise InvitationConflictError(
                        f"o e-mail {normalized} já pertence a um membro ativo da organização"
                    )

                # 2. Verifica se já existe convite pendente para este e-mail
                cursor = await connection.execute(
                    """
                    SELECT 1
                    FROM organization_invitations
                    WHERE organization_id = %s
                      AND normalized_email = %s
                    LIMIT 1
                    """,
                    (self.tenant.organization_id, normalized),
                )
                if await cursor.fetchone() is not None:
                    raise InvitationConflictError(
                        f"já existe um convite pendente para o e-mail {normalized}"
                    )

                # 3. Insere o convite
                cursor = await connection.execute(
                    """
                    INSERT INTO organization_invitations (
                        organization_id, normalized_email, role, invited_by_user_id, created_at
                    )
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    RETURNING organization_id, normalized_email, role, invited_by_user_id, created_at
                    """,
                    (self.tenant.organization_id, normalized, clean_role, invited_by),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("falha ao criar convite")
                return InvitationRecord(
                    organization_id=row[0],
                    normalized_email=str(row[1]),
                    role=str(row[2]),
                    invited_by_user_id=row[3],
                    created_at=row[4],
                )
        except Exception as exc:
            import psycopg.errors

            if isinstance(exc, psycopg.errors.UniqueViolation):
                diag = getattr(exc, "diag", None)
                constraint = getattr(diag, "constraint_name", "") or ""
                if constraint in {"pk_organization_invitations", "organization_invitations_pkey"} or "organization_invitations" in str(exc):
                    raise InvitationConflictError(
                        f"já existe um convite pendente para o e-mail {normalized}"
                    ) from exc
            raise

    async def cancel_invitation(
        self,
        email: str,
        actor_principal: Optional["RequestPrincipal"] = None,
    ) -> bool:
        """Cancela/remove um convite pendente por e-mail normalizado."""
        normalized = normalize_email(email)

        async with self.database.connection(self.tenant) as connection:
            if actor_principal is not None:
                cursor = await connection.execute(
                    """
                    SELECT role
                    FROM organization_invitations
                    WHERE organization_id = %s
                      AND normalized_email = %s
                    """,
                    (self.tenant.organization_id, normalized),
                )
                row = await cursor.fetchone()
                if row is None:
                    return False
                target_role = str(row[0])
                if not actor_principal.can_manage_role(target_role):
                    raise PermissionError(
                        f"papel {actor_principal.role} não tem permissão para cancelar convite com papel {target_role}"
                    )

            cursor = await connection.execute(
                """
                DELETE FROM organization_invitations
                WHERE organization_id = %s
                  AND normalized_email = %s
                """,
                (self.tenant.organization_id, normalized),
            )
            return cursor.rowcount > 0

    async def claim_invitation(
        self,
        user_id: UUID,
        user_subject: str,
        email: str,
        display_name: Optional[str] = None,
    ) -> Optional[str]:
        """Consome atomicamente o convite correspondente ao e-mail se existente."""
        normalized = normalize_email(email)
        async with self.database.connection(self.tenant) as connection:
            cursor = await connection.execute(
                """
                SELECT public.claim_organization_invitation(%s, %s, %s, %s, %s)
                """,
                (
                    self.tenant.organization_id,
                    user_id,
                    user_subject,
                    normalized,
                    display_name,
                ),
            )
            row = await cursor.fetchone()
            if row is not None and row[0] is not None:
                return str(row[0])
            return None


class InMemoryInvitationRepository:
    """Implementação em memória para desenvolvimento local offline / testes sem Postgres."""

    def __init__(self, tenant: TenantContext) -> None:
        self.tenant = tenant
        self._invitations: dict[str, InvitationRecord] = {}
        self._lock = asyncio.Lock()

    async def list_invitations(self) -> list[InvitationRecord]:
        async with self._lock:
            return sorted(
                self._invitations.values(),
                key=lambda x: x.created_at,
                reverse=True,
            )

    async def create_invitation(
        self,
        email: str,
        role: str,
        actor_principal: Optional["RequestPrincipal"] = None,
    ) -> InvitationRecord:
        normalized = normalize_email(email)
        clean_role = validate_role(role)
        if actor_principal is not None and not actor_principal.can_manage_role(clean_role):
            raise PermissionError(
                f"papel {actor_principal.role} não tem permissão para gerenciar o papel {clean_role}"
            )
        async with self._lock:
            from orchestrator.db.members import _get_in_memory_member_repository

            member_repo = _get_in_memory_member_repository(self.tenant)
            for m in member_repo._members.values():
                if m.email and m.email.strip().lower() == normalized:
                    raise InvitationConflictError(
                        f"o e-mail {normalized} já pertence a um membro ativo da organização"
                    )

            if normalized in self._invitations:
                raise InvitationConflictError(
                    f"já existe um convite pendente para o e-mail {normalized}"
                )

            inv = InvitationRecord(
                organization_id=self.tenant.organization_id,
                normalized_email=normalized,
                role=clean_role,
                invited_by_user_id=actor_principal.user_id if actor_principal else None,
                created_at=datetime.now(timezone.utc),
            )
            self._invitations[normalized] = inv
            return inv

    async def cancel_invitation(
        self,
        email: str,
        actor_principal: Optional["RequestPrincipal"] = None,
    ) -> bool:
        normalized = normalize_email(email)
        async with self._lock:
            if normalized not in self._invitations:
                return False
            inv = self._invitations[normalized]
            if actor_principal is not None and not actor_principal.can_manage_role(inv.role):
                raise PermissionError(
                    f"papel {actor_principal.role} não tem permissão para cancelar convite com papel {inv.role}"
                )
            del self._invitations[normalized]
            return True

    async def claim_invitation(
        self,
        user_id: UUID,
        user_subject: str,
        email: str,
        display_name: Optional[str] = None,
    ) -> Optional[str]:
        normalized = normalize_email(email)
        async with self._lock:
            if normalized not in self._invitations:
                return None
            inv = self._invitations.pop(normalized)
            from orchestrator.db.members import (
                MemberRecord,
                _get_in_memory_member_repository,
            )

            member_repo = _get_in_memory_member_repository(self.tenant)
            member_repo._members[user_subject] = MemberRecord(
                id=str(user_id),
                user_id=user_id,
                subject=user_subject,
                email=normalized,
                display_name=display_name or user_subject.capitalize(),
                role=inv.role,
                created_at=datetime.now(timezone.utc),
            )
            return inv.role


_IN_MEMORY_INVITATION_REPOSITORIES: dict[str, InMemoryInvitationRepository] = {}


def _get_in_memory_invitation_repository(
    tenant: TenantContext,
) -> InMemoryInvitationRepository:
    slug = tenant.organization_slug
    if slug not in _IN_MEMORY_INVITATION_REPOSITORIES:
        _IN_MEMORY_INVITATION_REPOSITORIES[slug] = InMemoryInvitationRepository(tenant)
    return _IN_MEMORY_INVITATION_REPOSITORIES[slug]


@asynccontextmanager
async def open_repository(
    database: Optional[Database] = None,
    tenant: Optional[TenantContext] = None,
) -> AsyncIterator[PostgresInvitationRepository | InMemoryInvitationRepository]:
    if database is not None and tenant is not None:
        yield PostgresInvitationRepository(database, tenant)
    elif os.environ.get("DATABASE_URL"):
        db = await get_shared_database()
        t = tenant or TenantIdentity.from_env().context()
        yield PostgresInvitationRepository(db, t)
    else:
        t = tenant or TenantIdentity.from_env().context()
        yield _get_in_memory_invitation_repository(t)
