from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.creative_contracts import CreatorRoster, ScriptResult
from orchestrator.tools.base import ToolContext
from orchestrator.tools.concepts import generate_concepts_tool
from orchestrator.tools.creator_profiles import design_creator_roster_tool
from orchestrator.tools.scripts import write_script_tool


class _NoInnerLlm:
    async def generate_concepts(self, **kwargs):
        raise AssertionError("agent submission must not call the LLM adapter again")

    async def write_script(self, **kwargs):
        raise AssertionError("agent submission must not call the LLM adapter again")


def _ctx() -> ToolContext:
    return ToolContext(
        adapter=_NoInnerLlm(),
        pipeline={},
        run={},
        run_id="run-agent",
    )


async def test_concept_submission_is_validated_without_an_inner_llm_call() -> None:
    result = await generate_concepts_tool(
        _ctx(),
        offer="Serum X",
        n=1,
        seed="run-agent",
        campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
        proposals=[
            {
                "hook": "The afternoon dryness check.",
                "angle": "Midday comfort",
                "audience_problem": "Skin feels dry after lunch",
                "product_mechanism": "Hydration",
                "evidence_basis": "cold_test",
                "format": "routine",
                "hook_style": "problem",
            }
        ],
    )

    assert result[0]["id"].startswith("concept-")
    assert result[0]["offer"] == "Serum X"
    assert "cost_usd" not in result[0]


async def test_script_submission_is_structured_without_an_inner_llm_call() -> None:
    result = await write_script_tool(
        _ctx(),
        concept={"id": "concept-1", "hook": "Try this", "angle": "Routine"},
        creator_ref="creator",
        platform="tiktok",
        return_contract=True,
        draft={
            "spoken_beats": [
                {"section": "hook", "text": "Try this", "seconds": 2},
                {"section": "body", "text": "One simple routine.", "seconds": 7},
            ],
            "visual_beats": ["Creator to camera"],
            "on_screen_text": ["Simple routine"],
            "call_to_action": "See the offer",
            "estimated_duration": 9,
        },
    )

    assert isinstance(result, ScriptResult)
    assert result.script_draft.concept_id == "concept-1"
    assert result.script.startswith("HOOK: Try this")


async def test_creator_profile_tool_builds_exactly_two_server_owned_profiles() -> None:
    result = await design_creator_roster_tool(
        _ctx(),
        campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 2},
        concept_ids=["concept-1", "concept-2"],
        creators=[
            {
                "archetype": "Warm guide",
                "visual_brief": "Adult creator in a bright bathroom.",
                "voice_brief": "Warm and practical.",
                "performance_style": "Calm.",
                "exclusions": [],
            },
            {
                "archetype": "Direct tester",
                "visual_brief": "Adult creator at a clean vanity.",
                "voice_brief": "Direct and energetic.",
                "performance_style": "Concise.",
                "exclusions": [],
            },
        ],
        assignments=[
            {"concept_id": "concept-1", "creator_index": 0},
            {"concept_id": "concept-2", "creator_index": 1},
        ],
    )

    assert isinstance(result, CreatorRoster)
    assert [creator.id for creator in result.creators] == ["creator-0", "creator-1"]


async def test_creator_profile_tool_rejects_unknown_assignment() -> None:
    with pytest.raises((ValidationError, ValueError)):
        await design_creator_roster_tool(
            _ctx(),
            campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
            concept_ids=["concept-1"],
            creators=[
                {
                    "archetype": "A",
                    "visual_brief": "Adult creator A.",
                    "voice_brief": "Warm.",
                    "performance_style": "Calm.",
                },
                {
                    "archetype": "B",
                    "visual_brief": "Adult creator B.",
                    "voice_brief": "Direct.",
                    "performance_style": "Fast.",
                },
            ],
            assignments=[{"concept_id": "unknown", "creator_index": 0}],
        )
