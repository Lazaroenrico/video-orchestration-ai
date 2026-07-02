"""ReplicateVoiceAdapter — TTS ElevenLabs hospedado no Replicate.

Usa ``replicate.async_run(ref, input=...)``, que resolve versão, faz o polling e
devolve o output pronto. Evita o contrato HTTP manual (campo ``version`` + header
``Prefer: wait`` + polling) que causava ``422``/``output: null``.

O modelo ElevenLabs no Replicate é obrigatório via ``REPLICATE_ELEVENLABS_MODEL``
ou pelo parâmetro ``model=``. O schema de input fica configurável porque modelos
Replicate podem variar o nome dos campos.

Nota: modelos *community* exigem o **version hash** pinado no ref
(``owner/name:version``) — sem versão, o SDK retorna 404. Sobrescreva via ``model=``.
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable, Optional

import replicate

from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters._retry import with_transport_retry
from orchestrator.tracing import traced

# Chaves conhecidas onde modelos de áudio costumam expor a saída.
_AUDIO_KEYS = ("audio_out", "audio", "output", "url")

Runner = Callable[..., Awaitable[Any]]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_base_input() -> dict[str, Any]:
    raw = _env("REPLICATE_ELEVENLABS_INPUT_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("REPLICATE_ELEVENLABS_INPUT_JSON precisa ser JSON válido") from exc
    if not isinstance(data, dict):
        raise RuntimeError("REPLICATE_ELEVENLABS_INPUT_JSON precisa ser um objeto JSON")
    return data


class ReplicateVoiceAdapter:
    """Cria áudio de voz de creator via modelo ElevenLabs no Replicate.

    Parameters
    ----------
    model:
        Ref do modelo ElevenLabs no Replicate (``owner/name`` ou ``owner/name:version``).
        Se ausente, lê ``REPLICATE_ELEVENLABS_MODEL``.
    runner:
        Async callable ``(ref, input=...) -> output`` injetável para testes.
        Default: ``replicate.async_run`` (lê ``REPLICATE_API_TOKEN`` do ambiente).
    """

    def __init__(
        self,
        model: Optional[str] = None,
        runner: Optional[Runner] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        text_field: Optional[str] = None,
        voice_field: Optional[str] = None,
        model_id_field: Optional[str] = None,
        base_input: Optional[dict[str, Any]] = None,
    ) -> None:
        resolved_model = (model or _env("REPLICATE_ELEVENLABS_MODEL")).strip()
        if not resolved_model:
            raise RuntimeError(
                "REPLICATE_ELEVENLABS_MODEL é obrigatório para TTS ElevenLabs via Replicate"
            )
        self.model = resolved_model
        self._runner: Runner = runner or replicate.async_run
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.text_field = (text_field or _env("REPLICATE_ELEVENLABS_TEXT_FIELD", "text")).strip()
        if not self.text_field:
            raise RuntimeError("REPLICATE_ELEVENLABS_TEXT_FIELD não pode ser vazio")
        self.voice_field = (voice_field or _env("REPLICATE_ELEVENLABS_VOICE_FIELD", "voice_id")).strip()
        self.model_id_field = (
            model_id_field or _env("REPLICATE_ELEVENLABS_MODEL_ID_FIELD", "model_id")
        ).strip()
        self.base_input = dict(base_input) if base_input is not None else _load_base_input()
        self.model_id = _env("REPLICATE_ELEVENLABS_MODEL_ID")
        self.voice_ids = {
            "female": _env("REPLICATE_ELEVENLABS_VOICE_ID_FEMALE"),
            "male": _env("REPLICATE_ELEVENLABS_VOICE_ID_MALE"),
            "neutral": _env("REPLICATE_ELEVENLABS_VOICE_ID_NEUTRAL"),
            "default": _env("REPLICATE_ELEVENLABS_VOICE_ID"),
        }

    @traced("adapter.replicate_voice.create_voice", run_type="tool", step=3, provider="replicate")
    async def create_voice(
        self, index: int, voice_profile: Optional[VoiceProfile] = None
    ) -> str:
        """Gera a referência de voz do creator ``index``. Retorna uma string (URL).

        Retenta em blips de conexão (``httpx.ConnectTimeout`` etc.); erros HTTP e de
        lógica propagam na hora.
        """
        output = await with_transport_retry(
            lambda: self._runner(
                self.model, input=self._build_input(index, voice_profile)
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.voice",
        )
        return self._coerce_output(output)

    def _build_input(self, index: int, voice_profile: Optional[VoiceProfile]) -> dict[str, Any]:
        body = dict(self.base_input)
        body[self.text_field] = self._build_text(index, voice_profile)

        voice_id = self._voice_id_for(voice_profile)
        if voice_id and self.voice_field:
            body[self.voice_field] = voice_id
        if self.model_id and self.model_id_field:
            body[self.model_id_field] = self.model_id
        return body

    def _voice_id_for(self, voice_profile: Optional[VoiceProfile]) -> str:
        preset = voice_profile.preset if voice_profile is not None else "neutral"
        return self.voice_ids.get(preset) or self.voice_ids["default"]

    @staticmethod
    def _build_text(index: int, voice_profile: Optional[VoiceProfile]) -> str:
        base_text = f"creator voice {index}"
        if voice_profile is None:
            return base_text
        if voice_profile.prompt:
            return f"{base_text} | preset={voice_profile.preset} | {voice_profile.prompt}"
        return f"{base_text} | preset={voice_profile.preset}"

    @staticmethod
    def _coerce_output(output: Any) -> str:
        """Normaliza o output (str | FileOutput | dict) para uma string."""
        if isinstance(output, dict):
            for key in _AUDIO_KEYS:
                if key in output:
                    return str(output[key])
            # fallback: primeiro valor do dict
            return str(next(iter(output.values())))
        return str(output)
