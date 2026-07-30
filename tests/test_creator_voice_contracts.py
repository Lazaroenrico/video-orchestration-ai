from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    FinalizedVoice,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact


def test_creator_voice_spec_validates_required_fields_and_ranges() -> None:
    spec = CreatorVoiceSpec(
        language_code="pt-BR",
        accent="neutral",
        vocal_presentation="feminine",
        vocal_age="adult",
        timbre="warm",
        pace="conversational",
        energy="balanced",
        warmth=0.8,
        expressiveness=0.6,
        rationale="Warm adult voice for cosmetic UGC",
    )

    assert spec.language_code == "pt-BR"
    assert spec.vocal_presentation == "feminine"
    assert spec.warmth == 0.8
    assert spec.use_case == "ugc_social"


def test_creator_voice_spec_rejects_out_of_bound_floats_or_invalid_enums() -> None:
    with pytest.raises(ValidationError):
        CreatorVoiceSpec(
            language_code="pt-BR",
            accent="neutral",
            vocal_presentation="invalid_gender",
            vocal_age="adult",
            timbre="warm",
            pace="conversational",
            energy="balanced",
            warmth=1.5,  # > 1.0
            expressiveness=0.5,
            rationale="Test rationale",
        )


def test_voice_candidate_and_batch_contracts() -> None:
    artifact = Artifact(kind="voice_candidate", uri="r2://runs/run-1/preview-1.mp3")
    candidate = VoiceCandidate(
        candidate_id="cand-1",
        preview=artifact,
        duration_seconds=5.2,
        media_type="audio/mpeg",
    )

    batch = VoiceDesignBatch(
        provider="elevenlabs",
        design_model="eleven_ttv_v3",
        description_hash="hash123",
        prompt_version="voice-match-v1",
        candidates=[candidate],
        cost_usd=0.05,
    )

    assert batch.candidates[0].candidate_id == "cand-1"
    assert batch.cost_usd == 0.05


def test_finalized_voice_contract() -> None:
    voice = FinalizedVoice(
        provider="elevenlabs",
        voice_ref="elevenlabs-voice-id-999",
        selected_candidate_id="cand-1",
        preview_uri="r2://runs/run-1/preview-1.mp3",
        design_model="eleven_ttv_v3",
        tts_model="eleven_turbo_v2_5",
    )

    assert voice.voice_ref == "elevenlabs-voice-id-999"
    assert voice.provider == "elevenlabs"


@pytest.mark.asyncio
async def test_mock_adapter_voice_design_methods() -> None:
    from orchestrator.adapters.mock import MockAdapter

    adapter = MockAdapter(tiers=[{"name": "standard", "rate": 0.01}])
    spec = CreatorVoiceSpec(
        vocal_presentation="feminine",
        vocal_age="adult",
        timbre="warm",
        pace="conversational",
        energy="balanced",
    )

    batch = await adapter.design_voice_candidates(spec)
    assert len(batch.candidates) == 3
    assert batch.cost_usd == 0.0

    finalized = await adapter.finalize_voice(
        batch.candidates[0].candidate_id,
        batch=batch,
        creator_id="creator-0",
        organization_id="org-1",
    )
    assert finalized.selected_candidate_id == batch.candidates[0].candidate_id
    assert "creator-0" in finalized.voice_ref
