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
        archetype_templates = [
            ("Warm routine guide", "Warm, conversational, and practical.", "Calm explanation with natural reactions."),
            ("Direct product tester", "Direct, energetic, and concise.", "Fast demonstration with a clear point of view."),
            ("Authentic storyteller", "Empathetic, personal, and engaging.", "Unfiltered personal experience and honest reaction."),
            ("Expert reviewer", "Clear, articulate, and confident.", "Detailed breakdown of features and benefits."),
        ]
        # The casting stage always produces the two reusable profiles promised by
        # the V2 creative-plan contract; assignments fan out across the batch.
        count = 2
        creators = []
        for idx in range(count):
            tpl_archetype, tpl_voice, tpl_style = archetype_templates[idx % len(archetype_templates)]
            creators.append({
                "archetype": f"{tpl_archetype} {idx + 1}" if count > len(archetype_templates) else tpl_archetype,
                "visual_brief": f"Adult creator {idx + 1}, approachable {direction} styling.",
                "voice_brief": tpl_voice,
                "performance_style": tpl_style,
                "exclusions": ["medical authority", "guaranteed outcomes"],
            })
        assignments = [
            {"concept_id": concept_id, "creator_index": index % count}
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
