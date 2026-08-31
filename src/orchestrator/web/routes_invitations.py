"""Rotas de convites para a organização (/api/v2/invitations)."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.auth import (
    Permission,
    RequestPrincipal,
    require_permission,
)
from orchestrator.db import invitations as invitation_store
from orchestrator.db.invitations import InvitationConflictError

_log = logging.getLogger(__name__)

router = APIRouter()


class CreateInvitationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=500)
    role: str = Field(pattern="^(owner|admin|member|viewer)$")


@router.get("/api/v2/invitations")
async def list_invitations(
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_READ)),
) -> dict[str, Any]:
    """Lista todos os convites pendentes da organização."""
    async with invitation_store.open_repository(tenant=principal.tenant) as repo:
        invitations = await repo.list_invitations()
    return {"invitations": [inv.to_dict() for inv in invitations]}


@router.post("/api/v2/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    req: CreateInvitationRequest,
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_WRITE)),
) -> dict[str, Any]:
    """Cria um novo convite para a organização."""
    if not principal.can_manage_role(req.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Papel {principal.role!r} não pode convidar para o papel {req.role!r}",
        )
    try:
        async with invitation_store.open_repository(tenant=principal.tenant) as repo:
            invitation = await repo.create_invitation(
                email=req.email,
                role=req.role,
                actor_principal=principal,
            )
    except InvitationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return {"ok": True, "invitation": invitation.to_dict()}


@router.delete("/api/v2/invitations/{email}")
async def cancel_invitation(
    email: str,
    principal: RequestPrincipal = Depends(require_permission(Permission.MEMBERS_WRITE)),
) -> dict[str, Any]:
    """Cancela um convite pendente por e-mail."""
    decoded_email = unquote(email).strip()
    try:
        async with invitation_store.open_repository(tenant=principal.tenant) as repo:
            success = await repo.cancel_invitation(
                email=decoded_email,
                actor_principal=principal,
            )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Convite para {decoded_email!r} não encontrado",
        )

    return {"ok": True}
