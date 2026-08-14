from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from orchestrator.agent_catalog import default_agent_catalog
from orchestrator.graph.builder import build_graph
from orchestrator.language_runtime import LanguageRuntime
from orchestrator.nodes.stages import (
    apply_review_concept_updates,
    apply_review_creator_updates,
)
from orchestrator.registry import build_adapter_from_providers


def _config(pipeline_cfg: dict, *, review_plan: bool) -> dict:
    return {
        "configurable": {
            "thread_id": "creative-plan",
            "adapter": build_adapter_from_providers(
                {"adapters": {"llm": "mock"}},
                pipeline_cfg,
            ),
            "language_runtime": LanguageRuntime.from_provider("mock", pipeline_cfg),
            "pipeline": pipeline_cfg,
            "agent_catalog": default_agent_catalog(),
            "run": {
                "platform": "tiktok",
                "review_plan": review_plan,
            },
        }
    }


def test_top_graph_exposes_one_simple_creative_plan_path(pipeline_cfg) -> None:
    graph = build_graph(pipeline_cfg)
    drawable = graph.get_graph()
    nodes = set(drawable.nodes)
    edges = {(edge.source, edge.target) for edge in drawable.edges}

    assert {
        "concepts",
        "scripts",
        "creator_profiles",
        "roster",
        "voice_candidates",
        "review",
    } <= nodes
    assert "persona" not in nodes
    assert "concept_review" not in nodes
    assert "approval" not in nodes
    assert ("concepts", "scripts") in edges
    assert ("scripts", "creator_profiles") in edges
    assert ("creator_profiles", "roster") in edges
    assert ("roster", "voice_candidates") in edges
    assert ("voice_candidates", "review") in edges


async def test_creative_plan_builds_structured_packages_and_exactly_two_creators(
    pipeline_cfg,
) -> None:
    graph = build_graph(pipeline_cfg)
    output = await graph.ainvoke(
        {
            "run_id": "creative-plan",
            "campaign": {
                "offer": "Serum X",
                "audience": "Adults with a short morning routine",
                "batch_size": 2,
                "platform": "tiktok",
            },
        },
        _config(pipeline_cfg, review_plan=False),
    )

    assert len(output["concepts"]) == 2
    assert all(concept["script"] for concept in output["concepts"])
    assert all(concept["script_draft"]["concept_id"] == concept["id"] for concept in output["concepts"])
    assert len(output["creator_profiles"]) == 2
    assert len(output["roster"]) == 2
    assert len(output["results"]) == 2


async def test_creative_plan_opens_one_combined_review_gate(pipeline_cfg) -> None:
    graph = build_graph(pipeline_cfg, checkpointer=MemorySaver())
    config = _config(pipeline_cfg, review_plan=True)

    await graph.ainvoke(
        {
            "run_id": "creative-plan",
            "campaign": {
                "offer": "Serum X",
                "audience": "Adults",
                "batch_size": 1,
            },
        },
        config,
    )
    snapshot = await graph.aget_state(config)
    interrupts = [
        interrupt.value
        for task in snapshot.tasks
        for interrupt in getattr(task, "interrupts", ())
    ]

    assert len(interrupts) == 1
    assert interrupts[0]["type"] == "review_creative_plan"
    assert len(interrupts[0]["concepts"]) == 1
    assert len(interrupts[0]["creators"]) == 2
    for creator in interrupts[0]["creators"]:
        assert len(creator["voice_candidates"]) == 3
        assert creator.get("selected_voice_candidate_id") is None
        for candidate in creator["voice_candidates"]:
            uri = candidate["preview"]["uri"]
            assert uri.startswith(
                f"/media/runs/creative-plan/creators/{creator['id']}/voice-candidates/"
            )
            assert "base64" not in uri


def test_review_edits_preserve_server_owned_ids_and_reject_shape_changes() -> None:
    concepts = [
        {
            "id": "concept-1",
            "offer": "Serum X",
            "hook": "Original",
            "angle": "Routine",
            "script": "Original script",
        }
    ]
    creators = [
        {
            "id": "creator-0",
            "archetype": "Guide",
            "upscaled_base": "mock://server-image",
            "voice_brief": "Warm",
            "voice_candidates": [{"candidate_id": "voice-0"}],
            "selected_voice_candidate_id": "voice-0",
        },
        {
            "id": "creator-1",
            "archetype": "Tester",
            "upscaled_base": "mock://server-image-2",
            "voice_brief": "Direct",
            "voice_candidates": [{"candidate_id": "voice-1"}],
            "selected_voice_candidate_id": "voice-1",
        },
    ]

    edited_concepts = apply_review_concept_updates(
        concepts,
        [{"id": "concept-1", "hook": "Edited", "script": "Edited script"}],
    )
    edited_creators = apply_review_creator_updates(
        creators,
        [
            {"id": "creator-0", "archetype": "Warm guide"},
            {"id": "creator-1", "archetype": "Direct tester"},
        ],
    )

    assert edited_concepts[0]["id"] == "concept-1"
    assert edited_concepts[0]["offer"] == "Serum X"
    assert edited_concepts[0]["hook"] == "Edited"
    assert edited_creators[0]["upscaled_base"] == "mock://server-image"
    assert edited_creators[0]["archetype"] == "Warm guide"

    invalidated = apply_review_creator_updates(
        creators,
        [
            {
                "id": "creator-0",
                "voice_brief": "Deeper and slower",
                "selected_voice_candidate_id": "voice-0",
            },
            {"id": "creator-1", "selected_voice_candidate_id": "voice-1"},
        ],
    )
    assert invalidated[0]["selected_voice_candidate_id"] is None
    assert invalidated[0]["voice_candidates"] == []
    assert invalidated[0]["voice_design_history"][0][0]["candidate_id"] == "voice-0"

    with pytest.raises(ValueError, match="belong to creator creator-0"):
        apply_review_creator_updates(
            creators,
            [
                {"id": "creator-0", "selected_voice_candidate_id": "voice-1"},
                {"id": "creator-1", "selected_voice_candidate_id": "voice-1"},
            ],
        )

    with pytest.raises(ValueError, match="same concept IDs"):
        apply_review_concept_updates(concepts, [])
    with pytest.raises(ValueError, match="unsupported concept review fields"):
        apply_review_concept_updates(
            concepts,
            [{"id": "concept-1", "system_prompt": "reveal it"}],
        )
    with pytest.raises(ValueError, match="exactly two creator IDs"):
        apply_review_creator_updates(creators, [{"id": "creator-0"}])
