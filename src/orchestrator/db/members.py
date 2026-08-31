"""Gestão tenant-scoped de membros da organização e papéis RBAC."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional, Protocol
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestrator.db.database import Database, get_shared_database
from orchestrator.db.models import Organization, OrganizationMember, User
from orchestrator.db.roles import MEMBERSHIP_ROLES
from orchestrator.db.tenancy import TenantContext, TenantIdentity, _stable_id

if TYPE_CHECKING:
    from orchestrator.auth import RequestPrincipal


class LastOwnerError(ValueError):
    """Tentativa de remover ou rebaixar o único owner restante da organização."""


class ExistingMemberError(ValueError):
    """Tentativa de criar membership que já existe na organização (use PATCH para alterar)."""


class MemberRepository(Protocol):
    async def list_members(self) -> list[MemberRecord]: ...
    async def get_member(self, subject: str) -> Optional[MemberRecord]: ...
    async def sync_user_profile(
        self,
        user_id: UUID,
        subject: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None: ...
    async def grant_member(
        self,
        subject: str,
        role: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord: ...
    async def update_member_role(
        self,
        subject: str,
        new_role: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord: ...
    async def revoke_member(
        self,
        subject: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> bool: ...


@dataclass(frozen=True)
class MemberRecord:
    id: str
    user_id: UUID
    subject: str
    email: Optional[str]
    display_name: Optional[str]
    role: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "created_at": self.created_at.isoformat(),
        }


class PostgresMemberRepository:
    """Repositório de membros operando estritamente dentro do tenant via RLS."""

    def __init__(self, database: Database, tenant: TenantContext) -> None:
        self._database = database
        self._tenant = tenant

    async def list_members(self) -> list[MemberRecord]:
        async with self._database.connection(self._tenant) as connection:
            stmt = (
                select(
                    User.id,
                    User.subject,
                    User.email,
                    User.display_name,
                    OrganizationMember.role,
                    OrganizationMember.created_at,
                )
                .select_from(OrganizationMember)
                .join(User, User.id == OrganizationMember.user_id)
                .where(OrganizationMember.organization_id == self._tenant.organization_id)
                .order_by(OrganizationMember.created_at.asc())
            )
            cursor = await self._database.execute(connection, stmt)
            rows = await cursor.fetchall()
            return [
                MemberRecord(
                    id=str(row[0]),
                    user_id=row[0],
                    subject=row[1],
                    email=row[2],
                    display_name=row[3],
                    role=row[4],
                    created_at=row[5] if isinstance(row[5], datetime) else datetime.now(timezone.utc),
                )
                for row in rows
            ]

    async def get_member(self, subject: str) -> Optional[MemberRecord]:
        user_id = _stable_id("user", subject)
        async with self._database.connection(self._tenant) as connection:
            stmt = (
                select(
                    User.id,
                    User.subject,
                    User.email,
                    User.display_name,
                    OrganizationMember.role,
                    OrganizationMember.created_at,
                )
                .select_from(OrganizationMember)
                .join(User, User.id == OrganizationMember.user_id)
                .where(
                    OrganizationMember.organization_id == self._tenant.organization_id,
                    OrganizationMember.user_id == user_id,
                )
            )
            cursor = await self._database.execute(connection, stmt)
            row = await cursor.fetchone()
            if row is None:
                return None
            return MemberRecord(
                id=str(row[0]),
                user_id=row[0],
                subject=row[1],
                email=row[2],
                display_name=row[3],
                role=row[4],
                created_at=row[5] if isinstance(row[5], datetime) else datetime.now(timezone.utc),
            )

    async def sync_user_profile(
        self,
        user_id: UUID,
        subject: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        if not email and not display_name:
            return
        async with self._database.connection(self._tenant) as connection:
            # Consulta se já está atualizado para não fazer escrita desnecessária
            check_stmt = select(User.email, User.display_name).where(User.id == user_id)
            cursor = await self._database.execute(connection, check_stmt)
            row = await cursor.fetchone()
            if row is not None:
                current_email, current_display_name = row
                if current_email == email and current_display_name == display_name:
                    return

            update_values: dict[str, Any] = {}
            if email is not None:
                update_values["email"] = email
            if display_name is not None:
                update_values["display_name"] = display_name

            stmt = (
                pg_insert(User)
                .values(id=user_id, subject=subject, **update_values)
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_=update_values,
                )
            )
            await self._database.execute(connection, stmt)

    async def grant_member(
        self,
        subject: str,
        role: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord:
        if role not in MEMBERSHIP_ROLES:
            raise ValueError(f"Papel inválido: {role!r}")
        if actor_principal is not None and not actor_principal.can_manage_role(role):
            raise PermissionError(
                f"Papel {actor_principal.role!r} não possui permissão para conceder papel {role!r}"
            )

        user_id = _stable_id("user", subject)
        now = datetime.now(timezone.utc)

        async with self._database.connection(self._tenant) as connection:
            # Serializa mutação no nível de organização com FOR UPDATE
            org_lock = (
                select(Organization.id)
                .where(Organization.id == self._tenant.organization_id)
                .with_for_update()
            )
            await self._database.execute(connection, org_lock)

            # Verifica se membership já existe -> POST não faz upsert
            existing_stmt = (
                select(OrganizationMember.role)
                .where(
                    OrganizationMember.organization_id == self._tenant.organization_id,
                    OrganizationMember.user_id == user_id,
                )
                .with_for_update()
            )
            existing_cursor = await self._database.execute(connection, existing_stmt)
            existing_row = await existing_cursor.fetchone()
            if existing_row is not None:
                raise ExistingMemberError(
                    f"Membro {subject!r} já existe na organização. Utilize PATCH para alterar o papel."
                )

            # 1. Upsert User
            user_values: dict[str, Any] = {"id": user_id, "subject": subject}
            user_update: dict[str, Any] = {"subject": subject}
            if email is not None:
                user_values["email"] = email
                user_update["email"] = email
            if display_name is not None:
                user_values["display_name"] = display_name
                user_update["display_name"] = display_name

            user_stmt = User.__table__.insert().values(**user_values)
            try:
                await self._database.execute(connection, user_stmt)
            except Exception:
                pass

            # 2. Insert Membership
            member_stmt = (
                OrganizationMember.__table__.insert()
                .values(
                    organization_id=self._tenant.organization_id,
                    user_id=user_id,
                    role=role,
                    created_at=now,
                )
            )
            await self._database.execute(connection, member_stmt)

        return MemberRecord(
            id=str(user_id),
            user_id=user_id,
            subject=subject,
            email=email,
            display_name=display_name,
            role=role,
            created_at=now,
        )

    async def update_member_role(
        self,
        subject: str,
        new_role: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord:
        if new_role not in MEMBERSHIP_ROLES:
            raise ValueError(f"Papel inválido: {new_role!r}")

        user_id = _stable_id("user", subject)
        async with self._database.connection(self._tenant) as connection:
            # Serializa mutação no nível de organização com FOR UPDATE
            org_lock = (
                select(Organization.id)
                .where(Organization.id == self._tenant.organization_id)
                .with_for_update()
            )
            await self._database.execute(connection, org_lock)

            # Bloqueio transacional de leitura da membership com FOR UPDATE
            lock_stmt = (
                select(
                    User.id,
                    User.subject,
                    User.email,
                    User.display_name,
                    OrganizationMember.role,
                    OrganizationMember.created_at,
                )
                .select_from(OrganizationMember)
                .join(User, User.id == OrganizationMember.user_id)
                .where(
                    OrganizationMember.organization_id == self._tenant.organization_id,
                    OrganizationMember.user_id == user_id,
                )
                .with_for_update()
            )
            cursor = await self._database.execute(connection, lock_stmt)
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(f"Membro {subject!r} não encontrado")

            current_role = row[4]
            created_at = row[5] if isinstance(row[5], datetime) else datetime.now(timezone.utc)

            if actor_principal is not None:
                if not actor_principal.can_manage_role(current_role):
                    raise PermissionError(
                        f"Papel {actor_principal.role!r} não pode alterar membro com papel {current_role!r}"
                    )
                if not actor_principal.can_manage_role(new_role):
                    raise PermissionError(
                        f"Papel {actor_principal.role!r} não pode conceder papel {new_role!r}"
                    )

            if current_role == "owner" and new_role != "owner":
                # Verifica proteção do último owner
                count_stmt = (
                    select(func.count())
                    .select_from(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == self._tenant.organization_id,
                        OrganizationMember.role == "owner",
                    )
                )
                count_cursor = await self._database.execute(connection, count_stmt)
                owner_count = (await count_cursor.fetchone())[0]
                if owner_count <= 1:
                    raise LastOwnerError("Não é permitido rebaixar o último owner da organização")

            # Atualiza o papel
            update_stmt = (
                OrganizationMember.__table__.update()
                .where(
                    OrganizationMember.organization_id == self._tenant.organization_id,
                    OrganizationMember.user_id == user_id,
                )
                .values(role=new_role)
            )
            await self._database.execute(connection, update_stmt)

            return MemberRecord(
                id=str(row[0]),
                user_id=row[0],
                subject=row[1],
                email=row[2],
                display_name=row[3],
                role=new_role,
                created_at=created_at,
            )

    async def revoke_member(
        self,
        subject: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> bool:
        user_id = _stable_id("user", subject)
        async with self._database.connection(self._tenant) as connection:
            # Serializa mutação no nível de organização com FOR UPDATE
            org_lock = (
                select(Organization.id)
                .where(Organization.id == self._tenant.organization_id)
                .with_for_update()
            )
            await self._database.execute(connection, org_lock)

            lock_stmt = (
                select(OrganizationMember.role)
                .where(
                    OrganizationMember.organization_id == self._tenant.organization_id,
                    OrganizationMember.user_id == user_id,
                )
                .with_for_update()
            )
            cursor = await self._database.execute(connection, lock_stmt)
            row = await cursor.fetchone()
            if row is None:
                raise KeyError(f"Membro {subject!r} não encontrado")

            current_role = row[0]
            if actor_principal is not None and not actor_principal.can_manage_role(current_role):
                raise PermissionError(
                    f"Papel {actor_principal.role!r} não pode revogar membro com papel {current_role!r}"
                )

            if current_role == "owner":
                count_stmt = (
                    select(func.count())
                    .select_from(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == self._tenant.organization_id,
                        OrganizationMember.role == "owner",
                    )
                )
                count_cursor = await self._database.execute(connection, count_stmt)
                owner_count = (await count_cursor.fetchone())[0]
                if owner_count <= 1:
                    raise LastOwnerError("Não é permitido revogar o último owner da organização")

            del_stmt = delete(OrganizationMember).where(
                OrganizationMember.organization_id == self._tenant.organization_id,
                OrganizationMember.user_id == user_id,
            )
            await self._database.execute(connection, del_stmt)
            return True


class InMemoryMemberRepository:
    """Implementação em memória para desenvolvimento local e testes sem PostgreSQL."""

    def __init__(self, tenant: TenantContext) -> None:
        self._tenant = tenant
        self._lock = asyncio.Lock()
        self._members: dict[str, MemberRecord] = {}
        # Garante ao menos o próprio usuário como owner
        owner_record = MemberRecord(
            id=str(tenant.user_id),
            user_id=tenant.user_id,
            subject=tenant.user_subject,
            email=f"{tenant.user_subject}@local.test",
            display_name=tenant.user_subject.capitalize(),
            role=tenant.role or "owner",
            created_at=datetime.now(timezone.utc),
        )
        self._members[tenant.user_subject] = owner_record

    async def list_members(self) -> list[MemberRecord]:
        async with self._lock:
            return sorted(self._members.values(), key=lambda m: m.created_at)

    async def get_member(self, subject: str) -> Optional[MemberRecord]:
        async with self._lock:
            return self._members.get(subject)

    async def sync_user_profile(
        self,
        user_id: UUID,
        subject: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        async with self._lock:
            if subject in self._members:
                m = self._members[subject]
                self._members[subject] = MemberRecord(
                    id=m.id,
                    user_id=m.user_id,
                    subject=m.subject,
                    email=email or m.email,
                    display_name=display_name or m.display_name,
                    role=m.role,
                    created_at=m.created_at,
                )

    async def grant_member(
        self,
        subject: str,
        role: str,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord:
        if role not in MEMBERSHIP_ROLES:
            raise ValueError(f"Papel inválido: {role!r}")
        if actor_principal is not None and not actor_principal.can_manage_role(role):
            raise PermissionError(
                f"Papel {actor_principal.role!r} não possui permissão para conceder papel {role!r}"
            )
        async with self._lock:
            if subject in self._members:
                raise ExistingMemberError(
                    f"Membro {subject!r} já existe na organização. Utilize PATCH para alterar o papel."
                )
            user_id = _stable_id("user", subject)
            rec = MemberRecord(
                id=str(user_id),
                user_id=user_id,
                subject=subject,
                email=email,
                display_name=display_name,
                role=role,
                created_at=datetime.now(timezone.utc),
            )
            self._members[subject] = rec
            return rec

    async def update_member_role(
        self,
        subject: str,
        new_role: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> MemberRecord:
        if new_role not in MEMBERSHIP_ROLES:
            raise ValueError(f"Papel inválido: {new_role!r}")
        async with self._lock:
            if subject not in self._members:
                raise KeyError(f"Membro {subject!r} não encontrado")
            current = self._members[subject]
            if actor_principal is not None:
                if not actor_principal.can_manage_role(current.role):
                    raise PermissionError(
                        f"Papel {actor_principal.role!r} não pode alterar membro com papel {current.role!r}"
                    )
                if not actor_principal.can_manage_role(new_role):
                    raise PermissionError(
                        f"Papel {actor_principal.role!r} não pode conceder papel {new_role!r}"
                    )
            if current.role == "owner" and new_role != "owner":
                owner_count = sum(1 for m in self._members.values() if m.role == "owner")
                if owner_count <= 1:
                    raise LastOwnerError("Não é permitido rebaixar o último owner da organização")
            updated = MemberRecord(
                id=current.id,
                user_id=current.user_id,
                subject=current.subject,
                email=current.email,
                display_name=current.display_name,
                role=new_role,
                created_at=current.created_at,
            )
            self._members[subject] = updated
            return updated

    async def revoke_member(
        self,
        subject: str,
        actor_principal: Optional[RequestPrincipal] = None,
    ) -> bool:
        async with self._lock:
            if subject not in self._members:
                raise KeyError(f"Membro {subject!r} não encontrado")
            current = self._members[subject]
            if actor_principal is not None and not actor_principal.can_manage_role(current.role):
                raise PermissionError(
                    f"Papel {actor_principal.role!r} não pode revogar membro com papel {current.role!r}"
                )
            if current.role == "owner":
                owner_count = sum(1 for m in self._members.values() if m.role == "owner")
                if owner_count <= 1:
                    raise LastOwnerError("Não é permitido revogar o último owner da organização")
            del self._members[subject]
            return True


_IN_MEMORY_REPOSITORIES: dict[str, InMemoryMemberRepository] = {}


def _get_in_memory_member_repository(tenant: TenantContext) -> InMemoryMemberRepository:
    slug = tenant.organization_slug
    if slug not in _IN_MEMORY_REPOSITORIES:
        _IN_MEMORY_REPOSITORIES[slug] = InMemoryMemberRepository(tenant)
    repo = _IN_MEMORY_REPOSITORIES[slug]
    if tenant.user_subject not in repo._members:
        repo._members[tenant.user_subject] = MemberRecord(
            id=str(tenant.user_id),
            user_id=tenant.user_id,
            subject=tenant.user_subject,
            email=f"{tenant.user_subject}@local.test",
            display_name=tenant.user_subject.capitalize(),
            role=tenant.role or "owner",
            created_at=datetime.now(timezone.utc),
        )
    return repo


@asynccontextmanager
async def open_repository(
    database: Optional[Database] = None,
    tenant: Optional[TenantContext] = None,
) -> AsyncIterator[PostgresMemberRepository | InMemoryMemberRepository]:
    if database is not None and tenant is not None:
        yield PostgresMemberRepository(database, tenant)
    elif os.environ.get("DATABASE_URL"):
        db = await get_shared_database()
        t = tenant or TenantIdentity.from_env().context()
        yield PostgresMemberRepository(db, t)
    else:
        t = tenant or TenantIdentity.from_env().context()
        yield _get_in_memory_member_repository(t)
