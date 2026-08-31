"""Rotas do gate humano V2 (revisão, aprovação de creators e edição de conceitos).

Contrato preservado (ver AGENTS.md): exatamente um gate ``review_creative_plan``;
modo local resolve via Future no registro em memória, modo durável resolve via
gates de PostgreSQL com 409 (stale/inválido) e 410 (gate cancelado).
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

import orchestrator.job_store as job_store
from orchestrator.auth import Permission, RequestPrincipal, require_permission
from orchestrator.nodes.stages import (
    apply_review_concept_updates,
    apply_review_creator_updates,
    validate_voice_selections,
)
from orchestrator.web import runs_registry
from orchestrator.web.run_executor import _server_attr

router = APIRouter()


class ReviewConceptPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    offer: Optional[str] = Field(default=None, max_length=8000)
    hook: Optional[str] = Field(default=None, max_length=500)
    angle: Optional[str] = Field(default=None, max_length=1000)
    audience_problem: Optional[str] = Field(default=None, max_length=1000)
    product_mechanism: Optional[str] = Field(default=None, max_length=1000)
    evidence_basis: Optional[Literal["provided_fact", "performance", "cold_test"]] = None
    format: Optional[str] = Field(default=None, max_length=200)
    hook_style: Optional[str] = Field(default=None, max_length=200)
    script: Optional[str] = Field(default=None, max_length=12000)
    script_draft: Optional[dict[str, Any]] = None


class ReviewCreatorPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    archetype: Optional[str] = Field(default=None, max_length=500)
    visual_brief: Optional[str] = Field(default=None, max_length=2000)
    voice_brief: Optional[str] = Field(default=None, max_length=2000)
    performance_style: Optional[str] = Field(default=None, max_length=1000)
    exclusions: Optional[list[str]] = Field(default=None, max_length=20)
    selected_voice_candidate_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
    )


class ReviewV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    concepts: Optional[list[ReviewConceptPatch]] = None
    creators: Optional[list[ReviewCreatorPatch]] = None
    target: Optional[str] = None
    ids: list[str] = Field(default_factory=list, max_length=48)
    feedback: Optional[str] = Field(default=None, max_length=4000)
    gate_id: Optional[str] = None
    version: Optional[int] = Field(default=None, ge=1)
    gate_type: Optional[Literal["review_creative_plan"]] = None

    @model_validator(mode="after")
    def validate_action(self) -> "ReviewV2Request":
        if self.action == "approve":
            if self.target is not None or self.ids or self.feedback is not None:
                raise ValueError("approve does not accept regeneration fields")
            return self
        if self.action == "regenerate":
            if self.target not in {"concepts", "scripts", "creators", "voices"}:
                raise ValueError(
                    "regenerate requires target concepts, scripts, creators, or voices"
                )
            if not self.ids or len(self.ids) != len(set(self.ids)):
                raise ValueError("regenerate requires unique IDs")
            if self.target in {"concepts", "scripts"} and self.creators is not None:
                raise ValueError("concept regeneration does not accept creator edits")
            if self.target in {"creators", "voices"} and self.concepts is not None:
                raise ValueError("creator regeneration does not accept concept edits")
            return self
        raise ValueError("action must be approve or regenerate")

    def resolution(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action}
        if self.concepts is not None:
            payload["concepts"] = [
                concept.model_dump(exclude_none=True) for concept in self.concepts
            ]
        if self.creators is not None:
            payload["creators"] = [
                creator.model_dump(exclude_none=True) for creator in self.creators
            ]
        if self.action == "regenerate":
            payload.update(
                target=self.target,
                ids=self.ids,
                feedback=self.feedback or "",
            )
        return payload


def _validated_review_resolution(
    req: ReviewV2Request,
    pending_review: dict[str, Any],
) -> dict[str, Any]:
    resolution = req.resolution()
    concepts = pending_review.get("concepts")
    creators = pending_review.get("creators")
    if not isinstance(concepts, list) or not isinstance(creators, list):
        raise HTTPException(409, "payload canônico da revisão indisponível")

    try:
        if req.action == "approve":
            if req.concepts is not None:
                apply_review_concept_updates(
                    concepts,
                    resolution["concepts"],
                )
            reviewed_creators = creators
            if req.creators is not None:
                reviewed_creators = apply_review_creator_updates(
                    creators,
                    resolution["creators"],
                )
            validate_voice_selections(reviewed_creators)
        elif req.ids:
            available_ids = {
                str(item.get("id"))
                for item in (creators if req.target in {"creators", "voices"} else concepts)
                if isinstance(item, dict) and item.get("id")
            }
            if not set(req.ids).issubset(available_ids):
                raise ValueError("regeneration IDs must belong to the pending review")
            if req.creators is not None:
                apply_review_creator_updates(creators, resolution["creators"])
            if req.concepts is not None:
                apply_review_concept_updates(concepts, resolution["concepts"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return resolution


@router.post("/api/v2/runs/{run_id}/review")
async def review_run_v2(
    run_id: str,
    req: ReviewV2Request,
    _principal: RequestPrincipal = Depends(require_permission(Permission.RUNS_REVIEW)),
) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        if req.gate_type != "review_creative_plan":
            raise HTTPException(
                409,
                "gate_type=review_creative_plan é obrigatório",
            )
        from orchestrator.db import CancelledGateError, StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                requested_gate_id = uuid.UUID(req.gate_id)
                pending_gate = await jobs.get_pending_gate(run_id)
                if (
                    pending_gate is None
                    or pending_gate.gate_id != requested_gate_id
                    or pending_gate.version != req.version
                    or pending_gate.gate_type != req.gate_type
                ):
                    raise HTTPException(
                        409,
                        "gate não corresponde à revisão pendente deste run",
                    )
                resolution = _validated_review_resolution(
                    req,
                    pending_gate.payload,
                )
                resume = await jobs.resolve_gate(
                    requested_gate_id,
                    version=req.version,
                    resolution=resolution,
                )
            except HTTPException:
                raise
            except CancelledGateError as exc:
                raise HTTPException(410, str(exc)) from exc
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        _server_attr("_wake_web_embedded_runner")()
        return {"ok": True, "job_id": str(resume.job_id)}

    state = runs_registry.REGISTRY.get(run_id)
    future = (state or {}).get("review")
    if future is None or future.done():
        raise HTTPException(409, "nenhuma revisão pendente")
    pending_review = (state or {}).get("pending_review")
    if not isinstance(pending_review, dict):
        raise HTTPException(409, "payload canônico da revisão indisponível")
    resolution = _validated_review_resolution(req, pending_review)
    future.set_result(resolution)
    return {"ok": True}


class ApproveRequest(BaseModel):
    approved: list[str] = []
    gate_id: Optional[str] = None
    version: Optional[int] = None


@router.post("/api/approve/{run_id}/creators/{creator_id}/reroll-voice")
async def reroll_creator_voice(
    run_id: str,
    creator_id: str,
    _principal: RequestPrincipal = Depends(require_permission(Permission.RUNS_VOICE_REROLL)),
) -> dict[str, Any]:
    """Compatibility endpoint that resumes the combined gate's voice-only branch."""
    if os.environ.get("DATABASE_URL"):
        raise HTTPException(
            status_code=409,
            detail="use the versioned V2 review endpoint for durable voice rerolls",
        )
    state = runs_registry.REGISTRY.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    pending_review = state.get("pending_review")
    review_future = state.get("review")
    if not isinstance(pending_review, dict) or review_future is None or review_future.done():
        raise HTTPException(status_code=409, detail="nenhuma revisão pendente")
    creator_ids = {
        str(creator.get("id") or "")
        for creator in pending_review.get("creators") or []
        if isinstance(creator, dict)
    }
    if creator_id not in creator_ids:
        raise HTTPException(status_code=404, detail=f"creator {creator_id!r} not found")

    review_future.set_result(
        {
            "action": "regenerate",
            "target": "voices",
            "ids": [creator_id],
            "feedback": "",
        }
    )
    return {"ok": True, "queued": True, "creator_id": creator_id}


@router.post("/api/approve/{run_id}")
async def approve(
    run_id: str,
    req: ApproveRequest,
    _principal: RequestPrincipal = Depends(require_permission(Permission.RUNS_REVIEW)),
) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        from orchestrator.db import StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                resume = await jobs.resolve_gate(
                    uuid.UUID(req.gate_id),
                    version=req.version,
                    resolution={"approved": req.approved},
                )
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        _server_attr("_wake_web_embedded_runner")()
        return {"ok": True, "job_id": str(resume.job_id)}
    st = runs_registry.REGISTRY.get(run_id)
    fut = (st or {}).get("approval")
    if not fut or fut.done():
        raise HTTPException(409, "nenhuma aprovação pendente")
    fut.set_result(
        {
            "approved": req.approved,
            "creators": list((st or {}).get("pending_creators") or []),
        }
    )
    return {"ok": True}


class ConceptEditRequest(BaseModel):
    # Conceitos editados e INCLUÍDOS (os excluídos simplesmente não vêm na lista).
    # Cada item é o dict do conceito com o campo "script" já editado.
    concepts: list[dict[str, Any]] = []
    gate_id: Optional[str] = None
    version: Optional[int] = None


@router.post("/api/approve/{run_id}/concepts")
async def submit_concepts(
    run_id: str,
    req: ConceptEditRequest,
    _principal: RequestPrincipal = Depends(require_permission(Permission.RUNS_REVIEW)),
) -> dict[str, Any]:
    if os.environ.get("DATABASE_URL"):
        if req.gate_id is None or req.version is None:
            raise HTTPException(409, "gate_id e version são obrigatórios")
        from orchestrator.db import StaleGateError

        async with job_store.open_repository() as jobs:
            assert jobs is not None
            try:
                resume = await jobs.resolve_gate(
                    uuid.UUID(req.gate_id),
                    version=req.version,
                    resolution={"concepts": req.concepts},
                )
            except (ValueError, StaleGateError) as exc:
                raise HTTPException(409, str(exc)) from exc
        _server_attr("_wake_web_embedded_runner")()
        return {
            "ok": True,
            "count": len(req.concepts),
            "job_id": str(resume.job_id),
        }
    st = runs_registry.REGISTRY.get(run_id)
    fut = (st or {}).get("concept_edit")
    if not fut or fut.done():
        raise HTTPException(409, "nenhuma edição de conceitos pendente")
    fut.set_result({"concepts": req.concepts})
    return {"ok": True, "count": len(req.concepts)}
