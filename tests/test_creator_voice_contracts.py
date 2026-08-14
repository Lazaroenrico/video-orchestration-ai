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


@pytest.mark.asyncio
async def test_derive_creator_voice_spec_tool_gender_alignment() -> None:
    from orchestrator.tools.base import ToolContext
    from orchestrator.tools.creators import derive_creator_voice_spec_tool

    ctx = ToolContext(adapter=None, pipeline={}, run={}, run_id="run-1")

    # 1. Deve usar voice_profile.preset ("male") se gravado no criador
    spec_male = await derive_creator_voice_spec_tool(
        ctx,
        profile={
            "id": "creator-0",
            "voice_profile": {"preset": "male", "prompt": ""},
            "voice_brief": "Warm, conversational and practical.",
            "visual_brief": "Adult creator, casual style.",
        },
    )
    assert spec_male["vocal_presentation"] == "masculine"

    # 2. Deve ler palavras-chave de gênero do visual_brief ("mulher")
    spec_female_pt = await derive_creator_voice_spec_tool(
        ctx,
        profile={
            "id": "creator-1",
            "visual_brief": "Criadora mulher adulta, estilo natural.",
            "voice_brief": "Voz acolhedora e direta.",
        },
    )
    assert spec_female_pt["vocal_presentation"] == "feminine"

    # 3. Fallback por índice: creator-1 sem keywords deve derivar masculine (index 1 é ímpar)
    spec_fallback_male = await derive_creator_voice_spec_tool(
        ctx,
        profile={
            "id": "creator-1",
            "voice_brief": "Voz acolhedora e direta.",
            "visual_brief": "Criador neutro.",
        },
    )
    assert spec_fallback_male["vocal_presentation"] == "masculine"
