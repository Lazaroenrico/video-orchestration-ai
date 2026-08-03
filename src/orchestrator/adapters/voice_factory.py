"""Fábrica de Voice Adapters — resolve o adapter de voz dinamicamente a partir de pipeline.yaml."""
from __future__ import annotations

from typing import Any

from orchestrator.adapters.base import VoiceDesignPort, VoicePort
from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
from orchestrator.adapters.elevenlabs_voice_design import ElevenLabsVoiceDesignAdapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter


def build_voice_adapter(pipeline: dict[str, Any]) -> VoicePort | VoiceDesignPort:
    """Resolve o Voice Adapter com base nas configurações de voz em ``pipeline.yaml``.

    Suporta os modos:
    - ``designed`` + ``elevenlabs``: chamadas diretas via ``ElevenLabsVoiceDesignAdapter``
    - ``replicate``: voz ElevenLabs hospedada no Replicate via ``ReplicateVoiceAdapter``
    - ``mock``: dry-run offline via ``MockAdapter``
    - fallback legado: ``ElevenLabsVoiceAdapter``
    """
    if "voice" not in pipeline:
        return ElevenLabsVoiceAdapter()

    voice_config = pipeline["voice"]
    if not isinstance(voice_config, dict):
        raise ValueError("voice configuration must be a mapping")
    mode = voice_config.get("mode")
    provider = voice_config.get("provider")

    if mode == "mock" and provider == "mock":
        return MockAdapter(
            tiers=pipeline.get("tiers", []),
            latency=float(pipeline.get("latency", 0.0)),
        )

    if mode == "designed" and provider == "elevenlabs":
        retry = voice_config.get("retry") or {}
        if not isinstance(retry, dict):
            raise ValueError("voice.retry configuration must be a mapping")
        costs = voice_config.get("costs") or {}
        if not isinstance(costs, dict):
            raise ValueError("voice.costs configuration must be a mapping")
        return ElevenLabsVoiceDesignAdapter(
            design_model=voice_config.get("design_model", "eleven_ttv_v3"),
            tts_model=voice_config.get("tts_model", "eleven_turbo_v2_5"),
            timeout_seconds=float(voice_config.get("request_timeout_seconds", 120)),
            max_concurrency=int(voice_config.get("concurrency", 1)),
            max_candidates=int(voice_config.get("candidates_per_creator", 3)),
            max_retries=int(retry.get("max_retries", 3)),
            retry_backoff_seconds=float(retry.get("backoff_base_seconds", 1.0)),
            retry_max_delay_seconds=float(retry.get("max_delay_seconds", 60.0)),
            design_cost_per_candidate_usd=float(
                costs.get("design_per_candidate_usd", 0.01)
            ),
            tts_cost_per_1000_chars_usd=float(
                costs.get("tts_per_1000_chars_usd", 0.03)
            ),
            cost_source=str(costs.get("cost_source", "estimate")),
        )

    if provider == "replicate":
        return ReplicateVoiceAdapter()

    if mode == "legacy" and provider == "elevenlabs":
        return ElevenLabsVoiceAdapter()

    raise ValueError(
        "unsupported voice configuration: "
        f"mode={mode!r}, provider={provider!r}"
    )
