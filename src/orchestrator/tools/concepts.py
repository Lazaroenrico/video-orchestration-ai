"""Concept-generation tools."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator.creative_contracts import (
    CampaignInput,
    ConceptSubmission,
    materialize_concepts,
)
from orchestrator.tools.base import ToolContext, require_dict_list
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.generate_concepts",
    run_type="tool",
    tool_name="generate_concepts",
    role="llm",
    stage="concepts",
)
async def generate_concepts_tool(
    ctx: ToolContext,
    *,
    offer: str,
    n: int,
    seed: str,
    bias: Optional[list[str]] = None,
    revision: Optional[str] = None,
    persona: Optional[str] = None,
    campaign: Optional[dict[str, Any]] = None,
    proposals: Optional[list[dict[str, Any]]] = None,
    revision_feedback: Optional[str] = None,
    agent_submission: bool = False,
) -> list[dict[str, Any]]:
    add_trace_metadata(
        tool_name="generate_concepts",
        role="llm",
        stage="concepts",
        run_id=ctx.run_id,
    )
    if campaign is None and proposals is None and not agent_submission:
        kwargs: dict[str, Any] = {
            "offer": offer,
            "n": n,
            "seed": seed,
            "bias": bias,
            "revision": revision_feedback or revision,
        }
        if persona is not None:
            kwargs["persona"] = persona
        if ctx.language_runtime is None:
            raise RuntimeError("generate_concepts requires LanguageRuntime in ToolContext")
        generated = await ctx.language_runtime.generate_concepts(**kwargs)
        return require_dict_list(generated, tool_name="generate_concepts_tool")

    campaign_input = CampaignInput.model_validate(
        campaign
        or {
            "offer": offer,
            "audience": "General adult audience",
            "platform": ctx.run.get("platform", "tiktok"),
            "batch_size": n,
        }
    )
    if campaign_input.offer != offer or campaign_input.batch_size != n:
        raise ValueError("offer and batch size are server-owned")

    if proposals is None and agent_submission:
        raise ValueError("agent must submit proposals")
    if proposals is None:
        kwargs = {
            "offer": offer,
            "n": n,
            "seed": seed,
            "bias": bias,
            "revision": revision_feedback or revision,
        }
        if persona is not None:
            kwargs["persona"] = persona
        if ctx.language_runtime is None:
            raise RuntimeError("generate_concepts requires LanguageRuntime in ToolContext")
        generated = await ctx.language_runtime.generate_concepts(**kwargs)
        generated = require_dict_list(generated, tool_name="generate_concepts_tool")

        submissions = [
            ConceptSubmission(
                hook=str(concept.get("hook") or "Untitled hook"),
                angle=str(concept.get("angle") or "Campaign angle"),
                audience_problem=str(
                    concept.get("audience_problem")
                    or f"Problem described for {campaign_input.audience}"
                ),
                product_mechanism=str(
                    concept.get("product_mechanism") or campaign_input.offer
                ),
                evidence_basis=concept.get("evidence_basis") or "cold_test",
                format=str(concept.get("format") or "talking_head"),
                hook_style=str(concept.get("hook_style") or "problem"),
            )
            for concept in generated
        ]
    else:
        submissions = [
            ConceptSubmission.model_validate(proposal)
            for proposal in proposals
        ]

    return [
        concept.model_dump(mode="json")
        for concept in materialize_concepts(
            submissions,
            campaign=campaign_input,
            run_id=ctx.run_id,
        )
    ]
