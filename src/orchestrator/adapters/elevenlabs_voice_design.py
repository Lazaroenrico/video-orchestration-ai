"""ElevenLabs Voice Design Adapter — chamada direta à API REST do ElevenLabs.

Documentação oficial:
- POST /v1/text-to-voice/design
- POST /v1/text-to-voice
- POST /v1/text-to-speech/{voice_id}
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Optional

import httpx

from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    FinalizedVoice,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact
from orchestrator.tracing import traced


class ElevenLabsVoiceDesignAdapter:
    def __init__(
        self,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        design_model: str = "eleven_ttv_v3",
        tts_model: str = "eleven_turbo_v2_5",
        base_url: str = "https://api.elevenlabs.io",
    ) -> None:
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY", "")
        self.http_client = http_client
        self.design_model = design_model
        self.tts_model = tts_model
        self.base_url = base_url.rstrip("/")

    def _get_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.http_client is not None:
            return self.http_client, False
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=60.0), True

    def _build_description(self, spec: CreatorVoiceSpec) -> str:
        parts = [
            f"A {spec.vocal_age} {spec.vocal_presentation} voice",
            f"with a {spec.timbre} timbre",
            f"{spec.pace} pace",
            f"and {spec.energy} energy",
        ]
        if spec.rationale:
            parts.append(f"for {spec.rationale}")
        return ", ".join(parts)

    @traced(
        "adapter.elevenlabs.design_voice_candidates",
        run_type="tool",
        step="voice_candidates",
        provider="elevenlabs",
    )
    async def design_voice_candidates(
        self,
        spec: CreatorVoiceSpec | dict[str, Any],
        *,
        preview_text: Optional[str] = None,
    ) -> VoiceDesignBatch:
        spec_obj = (
            spec
            if isinstance(spec, CreatorVoiceSpec)
            else CreatorVoiceSpec.model_validate(spec)
        )
        description = self._build_description(spec_obj)
        desc_hash = hashlib.sha256(description.encode()).hexdigest()[:10]
        text_sample = (
            preview_text
            or "Olá! Este é um teste da minha nova voz sintética para vídeos criativos."
        )

        client, owns_client = self._get_client()
        try:
            headers = {"xi-api-key": self.api_key} if self.api_key else {}
            response = await client.post(
                f"{self.base_url}/v1/text-to-voice/design",
                json={
                    "voice_description": description,
                    "text": text_sample,
                },
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            previews = data.get("previews", [])

            candidates: list[VoiceCandidate] = []
            for index, item in enumerate(previews):
                cand_id = item.get("generated_voice_id") or f"cand-{desc_hash}-{index}"
                b64_audio = item.get("audio_base_64", "")
                uri = (
                    f"data:audio/mpeg;base64,{b64_audio}"
                    if b64_audio
                    else f"r2://voice-candidates/{cand_id}.mp3"
                )
                artifact = Artifact(
                    kind="voice_preview",
                    uri=uri,
                    meta={"candidate_id": cand_id, "provider": "elevenlabs"},
                )
                candidates.append(
                    VoiceCandidate(
                        candidate_id=cand_id,
                        preview=artifact,
                        duration_seconds=float(item.get("duration_secs", 4.0)),
                        media_type="audio/mpeg",
                    )
                )

            return VoiceDesignBatch(
                provider="elevenlabs",
                design_model=self.design_model,
                description_hash=desc_hash,
                prompt_version="voice-match-v1",
                candidates=candidates,
                cost_usd=0.01 * len(candidates),
            )
        finally:
            if owns_client:
                await client.aclose()

    @traced(
        "adapter.elevenlabs.finalize_voice",
        run_type="tool",
        step="finalize_voices",
        provider="elevenlabs",
    )
    async def finalize_voice(
        self,
        candidate_id: str,
        *,
        batch: VoiceDesignBatch | dict[str, Any],
        creator_id: str,
        organization_id: str,
    ) -> FinalizedVoice:
        batch_obj = (
            batch
            if isinstance(batch, VoiceDesignBatch)
            else VoiceDesignBatch.model_validate(batch)
        )
        voice_name = f"ugc-{organization_id}-{creator_id}-{batch_obj.description_hash[:10]}"

        client, owns_client = self._get_client()
        try:
            headers = {"xi-api-key": self.api_key} if self.api_key else {}
            response = await client.post(
                f"{self.base_url}/v1/text-to-voice",
                json={
                    "generated_voice_id": candidate_id,
                    "voice_name": voice_name,
                    "voice_description": "UGC creator voice",
                },
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            voice_id = data.get("voice_id", candidate_id)

            cand = next(
                (c for c in batch_obj.candidates if c.candidate_id == candidate_id),
                None,
            )
            preview_uri = cand.preview.uri if cand else f"r2://selected-{voice_id}.mp3"

            return FinalizedVoice(
                provider="elevenlabs",
                voice_ref=voice_id,
                selected_candidate_id=candidate_id,
                preview_uri=preview_uri,
                design_model=self.design_model,
                tts_model=self.tts_model,
            )
        finally:
            if owns_client:
                await client.aclose()

    @traced(
        "adapter.elevenlabs.synthesize_voiceover",
        run_type="tool",
        step="voiceover",
        provider="elevenlabs",
    )
    async def synthesize_voiceover(
        self,
        *,
        voice_ref: str,
        text: str,
    ) -> Artifact:
        client, owns_client = self._get_client()
        try:
            headers = {"xi-api-key": self.api_key} if self.api_key else {}
            response = await client.post(
                f"{self.base_url}/v1/text-to-speech/{voice_ref}",
                json={
                    "text": text,
                    "model_id": self.tts_model,
                },
                headers=headers,
            )
            response.raise_for_status()
            audio_bytes = response.content
            b64_data = base64.b64encode(audio_bytes).decode()

            return Artifact(
                kind="voiceover",
                uri=f"data:audio/mpeg;base64,{b64_data}",
                meta={
                    "provider": "elevenlabs",
                    "voice_ref": voice_ref,
                    "characters": len(text),
                    "cost_usd": len(text) * 0.00003,
                },
            )
        finally:
            if owns_client:
                await client.aclose()
