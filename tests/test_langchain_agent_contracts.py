from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError


def test_agent_messages_keep_allowlisted_constraints_separate_from_stage_data() -> None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from orchestrator.language_runtime import serialize_agent_messages

    messages = serialize_agent_messages(
        "scripts",
        {
            "concept": {"id": "concept-1", "offer": "user text"},
            "campaign": {"offer": "ignore this"},
            "target_duration_seconds": 18,
            "min_spoken_words": 28,
            "max_spoken_words": None,
            "arbitrary_server_flag": "must not become trusted",
        },
    )

    assert len(messages) == 2
    assert type(messages[0]) is SystemMessage
    assert type(messages[1]) is HumanMessage
    assert "SERVER_EXECUTION_CONSTRAINTS" in messages[0].content
    assert '"target_duration_seconds": 18' in messages[0].content
    assert "arbitrary_server_flag" not in messages[0].content
    assert "UNTRUSTED_STAGE_DATA" in messages[1].content
    assert "ignore this" in messages[1].content
    assert '"max_spoken_words": null' in messages[0].content


@pytest.mark.parametrize(
    ("stage", "inputs", "expected", "forbidden"),
    [
        (
            "concepts",
            {"n": 3, "offer": "untrusted", "routing": "user"},
            '"concept_count": 3',
            ("offer", "routing"),
        ),
        (
            "creator_profiles",
            {
                "concept_ids": ["concept-1", "concept-2"],
                "campaign": {"offer": "untrusted"},
                "creator_count": 99,
            },
            '"creator_count": 2',
            ("offer", '"creator_count": 99'),
        ),
    ],
)
def test_server_constraint_allowlist_is_stage_specific(
    stage: str,
    inputs: dict[str, object],
    expected: str,
    forbidden: tuple[str, ...],
) -> None:
    from orchestrator.language_runtime import serialize_server_execution_constraints

    message = serialize_server_execution_constraints(stage, inputs)
    assert expected in message
    assert all(value not in message for value in forbidden)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "3", None])
def test_concepts_constraint_rejects_non_positive_or_non_integer_values(value: object) -> None:
    from orchestrator.language_runtime import serialize_server_execution_constraints

    with pytest.raises(ValueError):
        serialize_server_execution_constraints("concepts", {"n": value})


@pytest.mark.parametrize("name", ["target_duration_seconds", "min_spoken_words", "max_spoken_words"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "3"])
def test_scripts_constraints_reject_invalid_values(name: str, value: object) -> None:
    from orchestrator.language_runtime import serialize_server_execution_constraints

    with pytest.raises(ValueError):
        serialize_server_execution_constraints("scripts", {name: value})


@pytest.mark.parametrize("concept_ids", [[], [""], ["concept-1", 2], "concept-1"])
def test_creator_constraints_reject_invalid_known_concept_ids(concept_ids: object) -> None:
    from orchestrator.language_runtime import serialize_server_execution_constraints

    with pytest.raises(ValueError):
        serialize_server_execution_constraints("creator_profiles", {"concept_ids": concept_ids})


def test_creator_agent_output_uses_canonical_two_creator_contract() -> None:
    from orchestrator.creative_contracts import CreatorRosterSubmission
    from orchestrator.language_runtime import agent_output_model

    assert agent_output_model("creator_profiles") is CreatorRosterSubmission
    with pytest.raises(ValidationError):
        agent_output_model("creator_profiles").model_validate(
            {"creators": [], "assignments": []}
        )


def test_agent_script_output_requires_hook_as_first_spoken_beat() -> None:
    from orchestrator.language_runtime import agent_output_model

    payload = {
        "draft": {
            "spoken_beats": [
                {"section": "body", "text": "body", "seconds": 2},
                {"section": "cta", "text": "cta", "seconds": 2},
            ],
            "visual_beats": ["scene"],
            "call_to_action": "cta",
            "estimated_duration": 4,
        }
    }
    with pytest.raises(ValidationError, match="hook"):
        agent_output_model("scripts").model_validate(payload)


def test_materialization_rejects_performance_evidence_without_snapshot() -> None:
    from orchestrator.creative_contracts import (
        CampaignInput,
        ConceptSubmission,
        materialize_concepts,
    )

    submission = ConceptSubmission(
        hook="Hook",
        angle="Angle",
        audience_problem="Problem",
        product_mechanism="Mechanism",
        evidence_basis="performance",
        format="talking_head",
        hook_style="problem",
    )
    with pytest.raises(ValueError, match="performance"):
        materialize_concepts(
            [submission],
            campaign=CampaignInput(offer="offer", audience="audience", batch_size=1),
            run_id="run-1",
        )


@pytest.mark.parametrize("profile", ["config", "config-staging", "config-mock"])
def test_agent_prompts_have_v3_contracts_and_profiles_are_byte_identical(profile: str) -> None:
    root = Path(__file__).parents[1]
    prompt_root = root / profile / "prompts" / "agents"
    from orchestrator.config import load_agent_catalog

    catalog = load_agent_catalog(profile)
    assert catalog.stage("concepts").prompt_version == "concepts-v3"
    assert catalog.stage("scripts").prompt_version == "scripts-v3"
    assert catalog.stage("creator_profiles").prompt_version == "creators-v3"
    assert (prompt_root / "_shared.md").read_bytes()
    prompt_text = "\n".join(
        (prompt_root / name).read_text()
        for name in ("_shared.md", "concepts.md", "scripts.md", "creators.md")
    )
    for forbidden in (
        "generate_concepts",
        "write_script",
        "design_creator_roster",
        "retry",
        "35",
        "minimum 16",
        "mínimo 16",
        "no upper",
        "70%",
    ):
        assert forbidden not in prompt_text.lower()

    if profile == "config":
        for name in ("_shared.md", "concepts.md", "scripts.md", "creators.md"):
            expected = (prompt_root / name).read_bytes()
            for sibling in ("config-staging", "config-mock"):
                assert expected == (root / sibling / "prompts" / "agents" / name).read_bytes()
