"""ElevenLabs Voice Design Adapter — chamada direta à API REST do ElevenLabs.

Documentação oficial:
- POST /v1/text-to-voice/design
- POST /v1/text-to-voice
- POST /v1/text-to-speech/{voice_id}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from typing import Any, Optional

import httpx

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.creative_contracts import (
    CreatorVoiceSpec,
    FinalizedVoice,
    VoiceCandidate,
    VoiceDesignBatch,
)
from orchestrator.graph.state import Artifact
from orchestrator.tracing import traced

_log = logging.getLogger(__name__)

DEFAULT_PREVIEW_TEXT = (
    "Olá! Este é um exemplo de demonstração da minha nova voz sintética gerada "
    "por inteligência artificial para vídeos criativos de UGC."
)
FINALIZED_VOICE_DESCRIPTION = "Synthetic voice for an AI UGC creator"


def voice_description(spec: CreatorVoiceSpec) -> str:
    parts = [
        f"A {spec.vocal_age} {spec.vocal_presentation} voice",
        f"with a {spec.timbre} timbre",
        f"{spec.pace} pace",
        f"and {spec.energy} energy",
    ]
    if spec.rationale:
        parts.append(f"for {spec.rationale}")
    return ", ".join(parts)


def voice_description_hash(spec: CreatorVoiceSpec) -> str:
    return hashlib.sha256(voice_description(spec).encode()).hexdigest()[:10]


def _check_response(response: httpx.Response, label: str) -> None:
    if response.is_error:
        request_id = (
            response.headers.get("request-id")
            or response.headers.get("x-request-id")
            or "unavailable"
        )
        _log.error(
            "ElevenLabs request failed label=%s status=%d request_id=%s",
            label,
            response.status_code,
            request_id,
        )
        response.raise_for_status()


class ElevenLabsVoiceDesignAdapter:
    def __init__(
        self,
        api_key: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        design_model: str = "eleven_ttv_v3",
        tts_model: str = "eleven_turbo_v2_5",
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 120.0,
        max_concurrency: int = 1,
        max_candidates: int = 3,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1.0,
        retry_max_delay_seconds: float = 60.0,
        design_cost_per_candidate_usd: float = 0.01,
        tts_cost_per_1000_chars_usd: float = 0.03,
        cost_source: str = "estimate",
    ) -> None:
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY", "")
        self.http_client = http_client
        self.design_model = design_model
        self.tts_model = tts_model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_concurrency = int(max_concurrency)
        self.max_candidates = int(max_candidates)
        self.max_retries = int(max_retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.retry_max_delay_seconds = float(retry_max_delay_seconds)
        self.design_cost_per_candidate_usd = float(design_cost_per_candidate_usd)
        self.tts_cost_per_1000_chars_usd = float(tts_cost_per_1000_chars_usd)
        self.cost_source = str(cost_source)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_concurrency != 1:
            raise ValueError("ElevenLabs Voice Design concurrency must be 1")
        if self.max_candidates < 1 or self.max_candidates > 3:
            raise ValueError("max_candidates must be between 1 and 3")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.design_cost_per_candidate_usd < 0 or self.tts_cost_per_1000_chars_usd < 0:
            raise ValueError("voice cost estimates must not be negative")
        if self.cost_source != "estimate":
            raise ValueError("direct ElevenLabs costs must use cost_source=estimate")
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    def _get_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self.http_client is not None:
            return self.http_client, False
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self.timeout_seconds,
        ), True

    def _build_description(self, spec: CreatorVoiceSpec) -> str:
        return voice_description(spec)

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        label: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        headers = {"xi-api-key": self.api_key} if self.api_key else {}

        async def send() -> httpx.Response:
            response = await client.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=headers,
            )
            _check_response(response, label)
            return response

        async with self._semaphore:
            return await with_transport_retry(
                send,
                max_retries=self.max_retries,
                backoff_base=self.retry_backoff_seconds,
                max_delay=self.retry_max_delay_seconds,
                label=f"elevenlabs:{label}",
            )

    @staticmethod
    def _voice_name(
        *,
        organization_id: str,
        creator_id: str,
        description_hash: str,
    ) -> str:
        return f"ugc-{organization_id}-{creator_id}-{description_hash[:10]}"

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
        desc_hash = voice_description_hash(spec_obj)
        text_sample = (preview_text or DEFAULT_PREVIEW_TEXT).strip()
        if len(text_sample) < 100 or len(text_sample) > 1000:
            raise ValueError("preview text must contain 100 to 1000 characters")

        client, owns_client = self._get_client()
        try:
            response = await self._post(
                client,
                "/v1/text-to-voice/design",
                label="text-to-voice/design",
                payload={
                    "voice_description": description,
                    "text": text_sample,
                    "model_id": self.design_model,
                },
            )
            data = response.json()
            previews = data.get("previews", [])
            if not isinstance(previews, list) or not 1 <= len(previews) <= 3:
                raise ValueError("ElevenLabs must return 1 to 3 voice candidates")

            candidates: list[VoiceCandidate] = []
            candidate_ids: set[str] = set()
            for index, item in enumerate(previews[: self.max_candidates]):
                if not isinstance(item, dict):
                    raise ValueError("voice candidate must be an object")
                cand_id = item.get("generated_voice_id")
                if not isinstance(cand_id, str) or not cand_id.strip():
                    raise ValueError("voice candidate is missing generated_voice_id")
                cand_id = cand_id.strip()
                if cand_id in candidate_ids:
                    raise ValueError("voice candidate IDs must be unique")
                candidate_ids.add(cand_id)
                b64_audio = item.get("audio_base_64")
                if not isinstance(b64_audio, str) or not b64_audio.strip():
                    raise ValueError("voice candidate is missing base64 audio")
                b64_audio = b64_audio.strip()
                try:
                    base64.b64decode(b64_audio, validate=True)
                except ValueError as exc:
                    raise ValueError("voice candidate contains invalid base64 audio") from exc
                uri = f"data:audio/mpeg;base64,{b64_audio}"
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
                cost_usd=self.design_cost_per_candidate_usd * len(candidates),
                cost_source=self.cost_source,
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
        cand = next(
            (c for c in batch_obj.candidates if c.candidate_id == candidate_id),
            None,
        )
        if cand is None:
            raise ValueError("selected candidate does not belong to voice design batch")
        voice_name = self._voice_name(
            organization_id=organization_id,
            creator_id=creator_id,
            description_hash=batch_obj.description_hash,
        )

        client, owns_client = self._get_client()
        try:
            response = await self._post(
                client,
                "/v1/text-to-voice",
                label="text-to-voice",
                payload={
                    "generated_voice_id": candidate_id,
                    "voice_name": voice_name,
                    "voice_description": FINALIZED_VOICE_DESCRIPTION,
                },
            )
            data = response.json()
            voice_id = data.get("voice_id")
            if not isinstance(voice_id, str) or not voice_id.strip():
                raise ValueError("ElevenLabs response is missing permanent voice_id")
            voice_id = voice_id.strip()

            return FinalizedVoice(
                provider="elevenlabs",
                voice_ref=voice_id,
                selected_candidate_id=candidate_id,
                preview_uri=cand.preview.uri,
                design_model=self.design_model,
                tts_model=self.tts_model,
            )
        finally:
            if owns_client:
                await client.aclose()

    async def reconcile_voice(
        self,
        candidate_id: str,
        *,
        batch: VoiceDesignBatch | dict[str, Any],
        creator_id: str,
        organization_id: str,
    ) -> FinalizedVoice:
        """Reconcile an uncertain create by its deterministic provider-side name."""
        batch_obj = (
            batch
            if isinstance(batch, VoiceDesignBatch)
            else VoiceDesignBatch.model_validate(batch)
        )
        candidate = next(
            (item for item in batch_obj.candidates if item.candidate_id == candidate_id),
            None,
        )
        if candidate is None:
            raise ValueError("selected candidate does not belong to voice design batch")
        expected_name = self._voice_name(
            organization_id=organization_id,
            creator_id=creator_id,
            description_hash=batch_obj.description_hash,
        )
        client, owns_client = self._get_client()
        try:
            response = await client.get(
                f"{self.base_url}/v1/voices",
                headers={"xi-api-key": self.api_key} if self.api_key else {},
            )
            _check_response(response, "voices/reconcile")
            voices = response.json().get("voices") or []
            matches = [
                voice
                for voice in voices
                if isinstance(voice, dict) and voice.get("name") == expected_name
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "uncertain voice finalization has no unique provider match"
                )
            voice_id = matches[0].get("voice_id")
            if not isinstance(voice_id, str) or not voice_id.strip():
                raise RuntimeError("reconciled voice is missing voice_id")
            return FinalizedVoice(
                provider="elevenlabs",
                voice_ref=voice_id.strip(),
                selected_candidate_id=candidate_id,
                preview_uri=candidate.preview.uri,
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
            response = await self._post(
                client,
                f"/v1/text-to-speech/{voice_ref}",
                label="text-to-speech",
                payload={
                    "text": text,
                    "model_id": self.tts_model,
                },
            )
            audio_bytes = response.content
            b64_data = base64.b64encode(audio_bytes).decode()

            return Artifact(
                kind="voiceover",
                uri=f"data:audio/mpeg;base64,{b64_data}",
                meta={
                    "provider": "elevenlabs",
                    "voice_ref": voice_ref,
                    "characters": len(text),
                    "cost_usd": (
                        len(text) * self.tts_cost_per_1000_chars_usd / 1000
                    ),
                    "cost_source": self.cost_source,
                },
            )
        finally:
            if owns_client:
                await client.aclose()
