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


class _LegacyLlm:
    async def generate_concepts(self, **_kwargs):
        return [{"hook": "Hook", "angle": "Angle"}]

    async def write_script(self, **_kwargs):
        return "HOOK: Hook\nCTA: See the offer"


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


async def test_script_submission_rejects_narration_longer_than_the_server_budget() -> None:
    with pytest.raises(ValueError, match="narration exceeds 14 seconds"):
        await write_script_tool(
            _ctx(),
            concept={"id": "concept-1", "hook": "Try this", "angle": "Routine"},
            creator_ref="creator",
            platform="tiktok",
            target_duration_seconds=14,
            max_spoken_words=35,
            draft={
                "spoken_beats": [
                    {"section": "hook", "text": "Try this first.", "seconds": 4},
                    {
                        "section": "body",
                        "text": "This explanation deliberately takes too long.",
                        "seconds": 11,
                    },
                ],
                "visual_beats": ["Creator to camera"],
                "on_screen_text": [],
                "call_to_action": "See the offer",
                "estimated_duration": 15,
            },
        )


async def test_script_submission_rejects_more_than_the_server_word_budget() -> None:
    with pytest.raises(ValueError, match="narration exceeds 5 spoken words"):
        await write_script_tool(
            _ctx(),
            concept={"id": "concept-1", "hook": "Try this", "angle": "Routine"},
            creator_ref="creator",
            platform="tiktok",
            target_duration_seconds=14,
            max_spoken_words=5,
            draft={
                "spoken_beats": [
                    {
                        "section": "hook",
                        "text": "One two three four five six.",
                        "seconds": 3,
                    },
                ],
                "visual_beats": ["Creator to camera"],
                "on_screen_text": [],
                "call_to_action": "See it",
                "estimated_duration": 3,
            },
        )


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


async def test_agent_tools_require_their_terminal_structured_submissions() -> None:
    with pytest.raises(ValueError, match="submit proposals"):
        await generate_concepts_tool(
            _ctx(),
            offer="Serum X",
            n=1,
            seed="run-agent",
            agent_submission=True,
        )

    with pytest.raises(ValueError, match="structured draft"):
        await write_script_tool(
            _ctx(),
            concept={"id": "concept-1"},
            creator_ref="creator",
            platform="tiktok",
            agent_submission=True,
        )

    with pytest.raises(ValueError, match="submit creators and assignments"):
        await design_creator_roster_tool(
            _ctx(),
            campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
            concept_ids=["concept-1"],
            agent_submission=True,
        )


async def test_legacy_tools_forward_persona_and_enforce_server_owned_campaign_fields() -> None:
    ctx = ToolContext(
        adapter=object(),
        language_runtime=_LegacyLlm(),
        pipeline={},
        run={},
        run_id="run-agent",
    )

    concepts = await generate_concepts_tool(
        ctx,
        offer="Serum X",
        n=1,
        seed="run-agent",
        campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
        persona="Busy parent",
    )
    script = await write_script_tool(
        ctx,
        concept=concepts[0],
        creator_ref="creator",
        platform="tiktok",
        persona="Busy parent",
    )

    assert script.startswith("HOOK:")
    untyped = await generate_concepts_tool(
        ctx,
        offer="Serum X",
        n=1,
        seed="run-agent",
        persona="Busy parent",
    )
    assert untyped[0]["hook"] == "Hook"
    with pytest.raises(ValueError, match="server-owned"):
        await generate_concepts_tool(
            ctx,
            offer="Different offer",
            n=1,
            seed="run-agent",
            campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
        )


async def test_script_tool_requires_a_server_owned_concept_id() -> None:
    ctx = ToolContext(
        adapter=object(),
        language_runtime=_LegacyLlm(),
        pipeline={},
        run={},
        run_id="run-agent",
    )

    with pytest.raises(ValueError, match="concept id is required"):
        await write_script_tool(
            ctx,
            concept={"hook": "Hook"},
            creator_ref="creator",
            platform="tiktok",
        )


async def test_creative_tools_fail_fast_without_language_runtime_in_legacy_mode() -> None:
    ctx = ToolContext(
        adapter=object(),
        language_runtime=None,
        pipeline={},
        run={},
        run_id="run-agent",
    )

    with pytest.raises(RuntimeError, match="generate_concepts requires LanguageRuntime"):
        await generate_concepts_tool(
            ctx,
            offer="Serum X",
            n=1,
            seed="run-agent",
        )

    with pytest.raises(RuntimeError, match="write_script requires LanguageRuntime"):
        await write_script_tool(
            ctx,
            concept={"id": "concept-1", "hook": "Hook"},
            creator_ref="creator",
            platform="tiktok",
        )

    with pytest.raises(RuntimeError, match="generate_concepts requires LanguageRuntime"):
        await generate_concepts_tool(
            ctx,
            offer="Serum X",
            n=1,
            seed="run-agent",
            persona="creator",
            campaign={"offer": "Serum X", "audience": "Adults", "batch_size": 1},
        )



async def test_write_script_tool_unstructured_text_and_min_words() -> None:
    class _ShortLlm:
        async def write_script(self, **_kwargs):
            return "This is a plain unformatted single line of text with very few words"

    ctx = ToolContext(
        adapter=object(),
        language_runtime=_ShortLlm(),
        pipeline={},
        run={},
        run_id="run-agent",
    )

    with pytest.raises(ValueError, match="narration requires at least 30 spoken words"):
        await write_script_tool(
            ctx,
            concept={"id": "concept-1", "hook": "Hook"},
            creator_ref="creator",
            platform="tiktok",
            min_spoken_words=30,
        )


