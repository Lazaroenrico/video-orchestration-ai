from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.creative_contracts import (
    CampaignInput,
    ConceptSubmission,
    CreatorRosterSubmission,
    PerformanceMetric,
    PerformanceSnapshot,
    ScriptSubmission,
    materialize_concepts,
    materialize_creator_roster,
    materialize_script,
    script_result_from_text,
)


def test_campaign_input_accepts_the_public_setup_template() -> None:
    campaign = CampaignInput(
        offer="Serum X, R$ 99, hydration without a greasy finish",
        audience="Adults with dry skin who want a short morning routine",
        facts_restrictions="Use only the supplied hydration claim.",
        creator_direction="Warm, practical delivery.",
        video_direction="Bathroom routine with product close-up.",
        platform="tiktok",
        objective="conversion",
        batch_size=3,
        performance=PerformanceSnapshot(
            metrics=[
                PerformanceMetric(
                    creative_id="previous-1",
                    impressions=1000,
                    clicks=80,
                    conversions=9,
                    spend_usd=42,
                )
            ]
        ),
    )

    assert campaign.batch_size == 3
    assert campaign.performance is not None
    assert campaign.performance.metrics[0].creative_id == "previous-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"offer": "", "audience": "buyers"},
        {"offer": "offer", "audience": ""},
        {"offer": "offer", "audience": "buyers", "batch_size": 0},
        {"offer": "offer", "audience": "buyers", "unknown": "instruction"},
    ],
)
def test_campaign_input_rejects_invalid_or_unknown_fields(payload: dict) -> None:
    with pytest.raises(ValidationError):
        CampaignInput.model_validate(payload)


def test_materialize_concepts_assigns_server_owned_ids_and_offer() -> None:
    campaign = CampaignInput(offer="Serum X", audience="Adults", batch_size=2)
    submissions = [
        ConceptSubmission(
            hook="My skin stopped feeling tight after lunch.",
            angle="A calmer midday skin routine",
            audience_problem="Dryness returns during the day",
            product_mechanism="Hydration",
            evidence_basis="cold_test",
            format="routine",
            hook_style="problem",
        ),
        ConceptSubmission(
            hook="The two-minute routine I actually repeat.",
            angle="Consistency over complexity",
            audience_problem="Long routines are abandoned",
            product_mechanism="Simple daily use",
            evidence_basis="provided_fact",
            format="pov",
            hook_style="curiosity",
        ),
    ]

    concepts = materialize_concepts(
        submissions,
        campaign=campaign,
        run_id="run-123",
    )

    assert [concept.id for concept in concepts] == [
        "concept-7c28f73e",
        "concept-6aae88f0",
    ]
    assert all(concept.offer == "Serum X" for concept in concepts)


def test_concept_submission_cannot_control_server_fields() -> None:
    with pytest.raises(ValidationError):
        ConceptSubmission.model_validate(
            {
                "id": "attacker-controlled",
                "hook": "Hook",
                "angle": "Angle",
                "audience_problem": "Problem",
                "product_mechanism": "Mechanism",
                "evidence_basis": "cold_test",
                "format": "pov",
                "hook_style": "curiosity",
            }
        )


def test_materialize_script_keeps_structured_and_rendered_contracts() -> None:
    submission = ScriptSubmission.model_validate(
        {
            "spoken_beats": [
                {"section": "hook", "text": "I changed one step.", "seconds": 2},
                {"section": "body", "text": "Here is what I use.", "seconds": 8},
            ],
            "visual_beats": ["Creator addresses camera", "Product close-up"],
            "on_screen_text": ["One simple step"],
            "call_to_action": "See the offer",
            "estimated_duration": 10,
        }
    )

    draft = materialize_script(
        submission,
        run_id="run-123",
        concept_id="run-123-concept-01",
    )

    assert draft.id == "run-123-script-01"
    assert draft.concept_id == "run-123-concept-01"
    assert "HOOK: I changed one step." in draft.render_text()
    assert draft.call_to_action == "See the offer"


def test_legacy_script_text_is_mirrored_into_a_typed_draft() -> None:
    result = script_result_from_text(
        "HOOK: Pare de desperdiçar produto\nBODY: Use duas gotas\nCTA: Compre agora",
        run_id="run-1",
        concept_id="concept-7",
    )

    assert result.script.startswith("HOOK:")
    assert result.script_draft.concept_id == "concept-7"
    assert [beat.section for beat in result.script_draft.spoken_beats] == [
        "hook",
        "body",
        "cta",
    ]


def test_creator_roster_is_exactly_two_and_assignments_reference_known_concepts() -> None:
    submission = CreatorRosterSubmission.model_validate(
        {
            "creators": [
                {
                    "archetype": "Warm routine guide",
                    "visual_brief": "Adult creator in a bright bathroom.",
                    "voice_brief": "Warm and conversational.",
                    "performance_style": "Calm and practical.",
                    "exclusions": ["medical authority"],
                },
                {
                    "archetype": "Direct product tester",
                    "visual_brief": "Adult creator at a clean vanity.",
                    "voice_brief": "Direct and energetic.",
                    "performance_style": "Concise demonstrations.",
                    "exclusions": ["guaranteed outcomes"],
                },
            ],
            "assignments": [
                {"concept_id": "run-123-concept-01", "creator_index": 0},
                {"concept_id": "run-123-concept-02", "creator_index": 1},
            ],
        }
    )

    roster = materialize_creator_roster(
        submission,
        run_id="run-123",
        concept_ids=["run-123-concept-01", "run-123-concept-02"],
    )

    assert [creator.id for creator in roster.creators] == ["creator-0", "creator-1"]
    assert roster.assignments[1].creator_id == "creator-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"creators": [], "assignments": []},
        {
            "creators": [
                {
                    "archetype": "Only one",
                    "visual_brief": "Adult creator.",
                    "voice_brief": "Warm.",
                    "performance_style": "Direct.",
                }
            ],
            "assignments": [],
        },
    ],
)
def test_creator_roster_submission_requires_exactly_two_profiles(payload: dict) -> None:
    with pytest.raises(ValidationError):
        CreatorRosterSubmission.model_validate(payload)
