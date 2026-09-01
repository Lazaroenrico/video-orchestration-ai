"""Rotas de perfil do usuário (/api/v2/me) e gestão de membros (/api/v2/members)."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.auth import (
    Permission,
    RequestPrincipal,
    get_current_principal,
    require_permission,
)
from orchestrator.db import members as member_store
from orchestrator.db.members import ExistingMemberError, LastOwnerError

_log = logging.getLogger(__name__)

router = APIRouter()


class MemberGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(owner|admin|member|viewer)$")
    email: Optional[str] = Field(default=None, max_length=500)
    display_name: Optional[str] = Field(default=None, max_length=500)


class MemberUpdateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(owner|admin|member|viewer)$")


@router.get("/api/v2/me")
async def get_me(
    principal: RequestPrincipal = Depends(get_current_principal),
) -> dict[str, Any]:
    """Retorna os dados do usuário autenticado, organização, papel e permissões."""
    email = principal.claims.get("email")
    display_name = principal.claims.get("name") or principal.claims.get("display_name")

    # Sincronização idempotente de perfil se dados estiverem disponíveis
    if email or display_name:
        try:
            async with member_store.open_repository(tenant=principal.tenant) as repo:
                await repo.sync_user_profile(
                    user_id=principal.user_id,
                    subject=principal.user_subject,
                    email=email,
                    display_name=display_name,
                )
        except Exception:  # noqa: BLE001 - sync falho não deve derrubar o /api/v2/me
            _log.debug("sync_user_profile falhou silenciosamente", exc_info=True)

    auth_mode = os.environ.get("ORCH_AUTH_MODE", "disabled")

    return {
        "id": str(principal.user_id),
        "subject": principal.user_subject,
        "email": email,
        "display_name": display_name,
        "organization": {
            "id": str(principal.organization_id),
            "slug": principal.organization_slug,
            "name": principal.organization_name,
        },
        "role": principal.role,
        "permissions": sorted([p.value for p in principal.permissions]),
        "auth_mode": auth_mode,
    }


@router.get("/api/v2/members")
async def list_members(
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_READ)),
) -> dict[str, Any]:
    """Lista todos os membros da organização."""
    async with member_store.open_repository(tenant=principal.tenant) as repo:
        members = await repo.list_members()
    return {"members": [m.to_dict() for m in members]}


@router.post("/api/v2/members")
async def grant_member(
    req: MemberGrantRequest,
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_WRITE)),
) -> dict[str, Any]:
    """Concede membership para um novo usuário na organização (rejeita se já existir)."""
    if not principal.can_manage_role(req.role):
        raise HTTPException(
            status_code=403,
            detail=f"Papel {principal.role!r} não pode conceder papel {req.role!r}",
        )
    try:
        async with member_store.open_repository(tenant=principal.tenant) as repo:
            member = await repo.grant_member(
                subject=req.subject,
                role=req.role,
                email=req.email,
                display_name=req.display_name,
                actor_principal=principal,
            )
    except ExistingMemberError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LastOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"ok": True, "member": member.to_dict()}


@router.patch("/api/v2/members/{subject}")
async def update_member_role(
    subject: str,
    req: MemberUpdateRoleRequest,
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_WRITE)),
) -> dict[str, Any]:
    """Atualiza o papel de um membro existente."""
    if not principal.can_manage_role(req.role):
        raise HTTPException(
            status_code=403,
            detail=f"Papel {principal.role!r} não pode conceder papel {req.role!r}",
        )
    try:
        async with member_store.open_repository(tenant=principal.tenant) as repo:
            member = await repo.update_member_role(
                subject=subject,
                new_role=req.role,
                actor_principal=principal,
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Membro {subject!r} não encontrado") from exc
    except LastOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {"ok": True, "member": member.to_dict()}


@router.delete("/api/v2/members/{subject}")
async def revoke_member(
    subject: str,
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_WRITE)),
) -> dict[str, Any]:
    """Revoga a membership de um usuário da organização."""
    try:
        async with member_store.open_repository(tenant=principal.tenant) as repo:
            await repo.revoke_member(subject=subject, actor_principal=principal)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Membro {subject!r} não encontrado") from exc
    except LastOwnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return {"ok": True}
