"""RealCreatorAdapter — compõe OpenAIImage + TopazUpscale + ElevenLabsVoice,
implementa o Protocol CreatorPort (src/orchestrator/adapters/base.py).

Fluxo de ``build_creator(index)``:
1. ``OpenAIImageAdapter.generate_face(index)`` → dict com ``primary`` (URL) e ``angles``
2. ``TopazUpscaleAdapter.upscale(primary_url)`` → URL upscalada 4x
3. ``ElevenLabsVoiceAdapter.create_voice(index)`` → voice_id

Retorna o mesmo shape que ``MockAdapter.build_creator``::

    {
        "id": f"creator-{index}",
        "angles": ["front", "3/4", "profile", "smile", "neutral"],
        "upscaled_base": "<url upscalada>",
        "voice_id": "<voice_id>",
    }
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
import replicate

from orchestrator.adapters._throttle import get_replicate_throttle
from orchestrator.adapters.base import VoicePort, VoiceProfile, resolve_voice_profile
from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
from orchestrator.adapters.openai_image import OpenAIImageAdapter, build_openai_image_vercel_adapter
from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
from orchestrator.adapters.topaz_upscale import TopazUpscaleAdapter
from orchestrator.tracing import traced

_log = logging.getLogger(__name__)


class RealCreatorAdapter:
    """Implementa CreatorPort compondo os 3 sub-adapters.

    Parameters
    ----------
    image:
        Instância de ``OpenAIImageAdapter``. Se ``None``, instancia o real.
    topaz:
        Instância de ``TopazUpscaleAdapter``. Se ``None``, instancia o real.
    voice:
        Instância de ``VoicePort`` compatível. Se ``None``, instancia ElevenLabs direto.
    """

    def __init__(
        self,
        image: Optional[OpenAIImageAdapter] = None,
        topaz: Optional[TopazUpscaleAdapter] = None,
        voice: Optional[VoicePort] = None,
    ) -> None:
        self.image = image if image is not None else OpenAIImageAdapter()
        self.topaz = topaz if topaz is not None else TopazUpscaleAdapter()
        self.voice = voice if voice is not None else ElevenLabsVoiceAdapter()

    @traced("adapter.creator_real.build_creator", run_type="chain", step=3, provider="creator_real")
    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]:
        """Constrói o creator reutilizável combinando imagem, upscale e voz.

        Retorna o mesmo shape que ``MockAdapter.build_creator``.
        """
        # Resolve o perfil de voz ANTES da imagem: o mesmo preset alimenta o prompt
        # de imagem (token de gênero brand-safe) e a criação de voz, garantindo que
        # a voz do creator case com a aparência gerada.
        resolved_voice = resolve_voice_profile(system_prompt, voice_profile)

        # A face gerada é o artefato mínimo: se generate_face falhar, não há o que
        # salvar e o erro propaga. Upscale e voz são best-effort — uma falha neles
        # (ConnectTimeout, indisponibilidade) NÃO pode descartar a face já gerada.
        face = await self.image.generate_face(
            index, system_prompt=system_prompt, voice_profile=resolved_voice
        )
        if "primary" not in face:
            raise RuntimeError(
                f"Image adapter response is missing 'primary'. Keys present: {sorted(face)}"
            )
        if "angles" not in face:
            raise RuntimeError(
                f"Image adapter response is missing 'angles'. Keys present: {sorted(face)}"
            )
        primary = face["primary"]

        try:
            upscaled = await self.topaz.upscale(primary)
        except Exception as exc:  # noqa: BLE001 — preserva a face; segue sem upscale
            _log.error("upscale falhou (creator-%d): %s; usando imagem original", index, exc)
            upscaled = primary

        try:
            voice_id = await self.voice.create_voice(index, voice_profile=resolved_voice)
        except Exception as exc:  # noqa: BLE001 — voz é opcional; imagem preservada
            _log.error("voz falhou (creator-%d): %s", index, exc)
            voice_id = ""

        creator = {
            "id": f"creator-{index}",
            "angles": face["angles"],
            "upscaled_base": upscaled,
            "voice_id": voice_id,
        }
        if resolved_voice is not None:
            creator["voice_profile"] = resolved_voice.as_dict()
        return creator

    @traced("adapter.creator_real.reroll_voice", run_type="chain", step=3, provider="creator_real")
    async def reroll_creator_voice(
        self,
        *,
        creator_id: Any,
        index: int,
        reroll_count: int,
        creator: dict[str, Any],
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]:
        """Gera uma voz NOVA para o creator, preservando a imagem e o gênero.

        O índice efetivo é ``index + reroll_count``: no pool de vozes do
        ``ReplicateVoiceAdapter`` (seleção por ``index % len(pool)``) isso avança
        para a próxima voz do gênero a cada reroll, sem repetir enquanto o pool
        comportar. ``voice_source_uri``/``voice_preview_uri`` são zerados para o
        caller re-persistir o áudio novo.
        """
        voice_id = await self.voice.create_voice(
            index + reroll_count, voice_profile=voice_profile
        )
        return {
            "voice_id": voice_id,
            "voice_ref": voice_id,
            "voice": voice_id,
            "voice_source_uri": None,
            "voice_preview_uri": None,
        }


def build_real_creator_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:
    """Fábrica que monta o RealCreatorAdapter lendo tokens do ambiente.

    Tokens vêm de variáveis de ambiente: ``OPENAI_API_KEY``,
    ``TOPAZ_API_KEY``, ``ELEVENLABS_API_KEY``.
    """
    return RealCreatorAdapter(
        image=OpenAIImageAdapter(),
        topaz=TopazUpscaleAdapter(),
        voice=ElevenLabsVoiceAdapter(),
    )


def build_real_creator_vercel_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:
    """Fábrica que monta RealCreatorAdapter com GPT Image 2 via Vercel AI Gateway.

    - OpenAI Image: roteado pelo Vercel Gateway (AI_GATEWAY_API_KEY).
    - Topaz Upscale: chamada direta à API Topaz (TOPAZ_API_KEY).
    - ElevenLabs Voice: chamada direta à API ElevenLabs (ELEVENLABS_API_KEY).
    """
    return RealCreatorAdapter(
        image=build_openai_image_vercel_adapter(pipeline),
        topaz=TopazUpscaleAdapter(),
        voice=ElevenLabsVoiceAdapter(),
    )


def build_real_creator_replicate_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:
    """Fábrica que monta RealCreatorAdapter usando Replicate para upscale e ElevenLabs.

    - OpenAI Image: roteado pelo Vercel Gateway (AI_GATEWAY_API_KEY).
    - Upscale: Replicate nightmareai/real-esrgan (REPLICATE_API_TOKEN).
    - Voice: modelo ElevenLabs hospedado no Replicate (REPLICATE_ELEVENLABS_MODEL).

    Usa um ``replicate.Client`` com timeout generoso — o rosto do GPT Image 2 vem
    como data URI base64 (~2.7MB) e é enviado inline; com cold start do modelo, o
    timeout padrão do client estoura (ReadTimeout).
    """
    rep_client = replicate.Client(
        api_token=os.environ.get("REPLICATE_API_TOKEN"),
        timeout=httpx.Timeout(600.0, connect=15.0),
    )
    # Throttle global: upscale e voz dividem o orçamento de rate limit da conta
    # com o adapter de vídeo (contas com crédito baixo têm burst 1).
    throttle = get_replicate_throttle()
    return RealCreatorAdapter(
        image=build_openai_image_vercel_adapter(pipeline),
        topaz=ReplicateUpscaleAdapter(runner=rep_client.async_run, throttle=throttle),
        voice=ReplicateVoiceAdapter(runner=rep_client.async_run, throttle=throttle),
    )
