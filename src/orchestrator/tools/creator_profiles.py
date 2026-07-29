"""Typed submission tool for the creator casting agent."""
from __future__ import annotations

from typing import Any, Optional

from orchestrator.creative_contracts import (
    CampaignInput,
    CreatorRoster,
    CreatorRosterSubmission,
    materialize_creator_roster,
)
from orchestrator.tools.base import ToolContext
from orchestrator.tracing import add_trace_metadata, traced


@traced(
    "tool.design_creator_roster",
    run_type="tool",
    tool_name="design_creator_roster",
    role="llm",
    stage="creator_profiles",
)
async def design_creator_roster_tool(
    ctx: ToolContext,
    *,
    campaign: dict[str, Any],
    concept_ids: list[str],
    creative_packages: Optional[list[dict[str, Any]]] = None,
    creators: Optional[list[dict[str, Any]]] = None,
    assignments: Optional[list[dict[str, Any]]] = None,
    revision_feedback: Optional[str] = None,
    agent_submission: bool = False,
) -> CreatorRoster:
    del creative_packages, revision_feedback
    campaign_input = CampaignInput.model_validate(campaign)
    add_trace_metadata(
        tool_name="design_creator_roster",
        role="llm",
        stage="creator_profiles",
        run_id=ctx.run_id,
    )
    if agent_submission and (creators is None or assignments is None):
        raise ValueError("agent must submit creators and assignments")
    if creators is None or assignments is None:
        direction = campaign_input.creator_direction or "commercial UGC"
        creators = [
            {
                "archetype": "Warm routine guide",
                "visual_brief": f"Adult creator, approachable {direction} styling.",
                "voice_brief": "Warm, conversational, and practical.",
                "performance_style": "Calm explanation with natural reactions.",
                "exclusions": ["medical authority", "guaranteed outcomes"],
            },
            {
                "archetype": "Direct product tester",
                "visual_brief": f"Adult creator, confident {direction} styling.",
                "voice_brief": "Direct, energetic, and concise.",
                "performance_style": "Fast demonstration with a clear point of view.",
                "exclusions": ["celebrity likeness", "guaranteed outcomes"],
            },
        ]
        assignments = [
            {"concept_id": concept_id, "creator_index": index % 2}
            for index, concept_id in enumerate(concept_ids)
        ]
    submission = CreatorRosterSubmission.model_validate(
        {"creators": creators, "assignments": assignments}
    )
    return materialize_creator_roster(
        submission,
        run_id=ctx.run_id,
        concept_ids=concept_ids,
    )
