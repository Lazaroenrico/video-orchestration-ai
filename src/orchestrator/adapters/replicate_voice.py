"""ReplicateVoiceAdapter — referência de voz de creator via SDK oficial ``replicate``.

Usa ``replicate.async_run(ref, input=...)``, que resolve versão, faz o polling e
devolve o output pronto. Evita o contrato HTTP manual (campo ``version`` + header
``Prefer: wait`` + polling) que causava ``422``/``output: null``.

Modelo padrão: ``suno-ai/bark`` (TTS) — input ``prompt`` (texto). O output pode
vir como string, ``FileOutput`` (URL-like) ou dict (ex.: ``{"audio_out": url}``);
``create_voice`` normaliza tudo para uma string (a referência de voz).

Nota: modelos *community* exigem o **version hash** pinado no ref
(``owner/name:version``) — sem versão, o SDK retorna 404. Sobrescreva via ``model=``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import replicate

from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters._retry import with_transport_retry
from orchestrator.tracing import traced

_DEFAULT_MODEL = (
    "suno-ai/bark:"
    "b76242b40d67c76ab6742e987628a2a9ac019e11d56ab96c4e91ce03b79b2787"
)
# Chaves conhecidas onde modelos de áudio costumam expor a saída.
_AUDIO_KEYS = ("audio_out", "audio", "output")

Runner = Callable[..., Awaitable[Any]]


class ReplicateVoiceAdapter:
    """Cria referência de voz de creator via Replicate.

    Parameters
    ----------
    model:
        Ref do modelo Replicate (``owner/name`` ou ``owner/name:version``).
    runner:
        Async callable ``(ref, input=...) -> output`` injetável para testes.
        Default: ``replicate.async_run`` (lê ``REPLICATE_API_TOKEN`` do ambiente).
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        runner: Optional[Runner] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self.model = model
        self._runner: Runner = runner or replicate.async_run
        self.max_retries = max_retries
        self.backoff_base = backoff_base

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
                self.model, input={"prompt": self._build_prompt(index, voice_profile)}
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.voice",
        )
        return self._coerce_output(output)

    @staticmethod
    def _build_prompt(index: int, voice_profile: Optional[VoiceProfile]) -> str:
        base_prompt = f"creator voice {index}"
        if voice_profile is None:
            return base_prompt
        if voice_profile.prompt:
            return f"{base_prompt} | preset={voice_profile.preset} | {voice_profile.prompt}"
        return f"{base_prompt} | preset={voice_profile.preset}"

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
