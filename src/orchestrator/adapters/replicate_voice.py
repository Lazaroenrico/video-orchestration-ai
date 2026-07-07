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
from orchestrator.adapters._throttle import AsyncThrottle
from orchestrator.tracing import traced

# Chaves conhecidas onde modelos de áudio costumam expor a saída.
_AUDIO_KEYS = ("audio_out", "audio", "output", "url")

Runner = Callable[..., Awaitable[Any]]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_pool(name: str) -> list[str]:
    """Lê ``name`` como lista separada por vírgula (um pool de vozes). Vazio -> ``[]``."""
    return [v.strip() for v in _env(name).split(",") if v.strip()]


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
        throttle: Optional[AsyncThrottle] = None,
    ) -> None:
        resolved_model = (model or _env("REPLICATE_ELEVENLABS_MODEL")).strip()
        if not resolved_model:
            raise RuntimeError(
                "REPLICATE_ELEVENLABS_MODEL é obrigatório para TTS ElevenLabs via Replicate"
            )
        self.model = resolved_model
        self._runner: Runner = runner or replicate.async_run
        self._throttle = throttle
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
        # Cada preset é um *pool* (lista) de vozes; a seleção por índice de creator dá
        # variedade determinística (voz distinta por creator do mesmo gênero) sem repetir.
        self.voice_pools = {
            "female": _env_pool("REPLICATE_ELEVENLABS_VOICE_ID_FEMALE"),
            "male": _env_pool("REPLICATE_ELEVENLABS_VOICE_ID_MALE"),
            "neutral": _env_pool("REPLICATE_ELEVENLABS_VOICE_ID_NEUTRAL"),
            "default": _env_pool("REPLICATE_ELEVENLABS_VOICE_ID"),
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
            lambda: self._throttled_run(
                self.model, input=self._build_input(index, voice_profile)
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.voice",
        )
        return self._coerce_output(output)

    async def _throttled_run(self, ref: str, **kwargs: Any) -> Any:
        """Passa cada tentativa pelo throttle global (quando configurado)."""
        if self._throttle is None:
            return await self._runner(ref, **kwargs)
        return await self._throttle.run(lambda: self._runner(ref, **kwargs))

    def _build_input(self, index: int, voice_profile: Optional[VoiceProfile]) -> dict[str, Any]:
        body = dict(self.base_input)
        body[self.text_field] = self._build_text(index, voice_profile)

        voice_id = self._voice_id_for(index, voice_profile)
        if voice_id and self.voice_field:
            body[self.voice_field] = voice_id
        if self.model_id and self.model_id_field:
            body[self.model_id_field] = self.model_id
        return body

    def _voice_id_for(self, index: int, voice_profile: Optional[VoiceProfile]) -> str:
        """Escolhe a voz do pool do preset ciclando por ``index`` (sem repetir enquanto
        o pool for >= nº de creators do gênero). Cai no pool ``default`` se o preset
        estiver vazio; ``""`` se não há nenhuma voz configurada."""
        preset = voice_profile.preset if voice_profile is not None else "neutral"
        pool = self.voice_pools.get(preset) or self.voice_pools["default"]
        if not pool:
            return ""
        return pool[index % len(pool)]

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
        """Normaliza o output (str | FileOutput | dict) para uma string.

        Output nulo/vazio é erro: coagir para ``str`` produziria ``"None"`` como
        voice_id, que seria persistido e quebraria o downstream em silêncio.
        """
        if output is None:
            raise RuntimeError("Replicate voice output is empty")
        if isinstance(output, dict):
            if not output:
                raise RuntimeError("Replicate voice output dict is empty")
            for key in _AUDIO_KEYS:
                if output.get(key):
                    return str(output[key])
            # fallback: primeiro valor do dict (nulo/vazio é erro, não "None")
            first = next(iter(output.values()))
            if not first:
                raise RuntimeError("Replicate voice output is empty")
            return str(first)
        uri = str(output).strip()
        if not uri:
            raise RuntimeError("Replicate voice output is empty")
        return uri
