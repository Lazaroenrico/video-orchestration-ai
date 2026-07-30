from __future__ import annotations

import httpx
import pytest

from orchestrator.adapters.elevenlabs_voice_design import ElevenLabsVoiceDesignAdapter
from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact


@pytest.mark.asyncio
async def test_elevenlabs_voice_design_adapter_calls_design_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/text-to-voice/design":
            return httpx.Response(
                200,
                json={
                    "previews": [
                        {
                            "generated_voice_id": "gen_voice_001",
                            "audio_base_64": "UklGRiQAAABXQVZFRm10IBAAAAABAAEARKwAAAB9AAACABAAZGF0YQAAAAA=",
                            "duration_secs": 3.5,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        spec = CreatorVoiceSpec(
            vocal_presentation="feminine",
            vocal_age="adult",
            timbre="warm",
            pace="conversational",
            energy="balanced",
        )
        batch = await adapter.design_voice_candidates(spec)
        assert len(batch.candidates) == 1
        assert batch.candidates[0].candidate_id == "gen_voice_001"


@pytest.mark.asyncio
async def test_elevenlabs_voice_design_adapter_calls_create_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/text-to-voice":
            return httpx.Response(
                200,
                json={"voice_id": "eleven_permanent_123"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = ElevenLabsVoiceDesignAdapter(api_key="test-key", http_client=client)
        candidate = VoiceCandidate(
            candidate_id="gen_voice_001",
            preview=Artifact(kind="voice_preview", uri="r2://preview.mp3"),
            duration_seconds=3.5,
        )
        batch = VoiceDesignBatch(
            provider="elevenlabs",
            design_model="eleven_ttv_v3",
            description_hash="deschash123",
            prompt_version="voice-match-v1",
            candidates=[candidate],
            cost_usd=0.01,
        )
        finalized = await adapter.finalize_voice(
            "gen_voice_001",
            batch=batch,
            creator_id="creator-0",
            organization_id="org-test",
        )
        assert finalized.voice_ref == "eleven_permanent_123"
