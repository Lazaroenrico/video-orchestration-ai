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

import os
from typing import Any, Optional

import httpx
import replicate

from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
from orchestrator.adapters.openai_image import OpenAIImageAdapter, build_openai_image_vercel_adapter
from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
from orchestrator.adapters.topaz_upscale import TopazUpscaleAdapter
from orchestrator.tracing import traced


class RealCreatorAdapter:
    """Implementa CreatorPort compondo os 3 sub-adapters.

    Parameters
    ----------
    image:
        Instância de ``OpenAIImageAdapter``. Se ``None``, instancia o real.
    topaz:
        Instância de ``TopazUpscaleAdapter``. Se ``None``, instancia o real.
    voice:
        Instância de ``ElevenLabsVoiceAdapter``. Se ``None``, instancia o real.
    """

    def __init__(
        self,
        image: Optional[OpenAIImageAdapter] = None,
        topaz: Optional[TopazUpscaleAdapter] = None,
        voice: Optional[ElevenLabsVoiceAdapter] = None,
    ) -> None:
        self.image = image if image is not None else OpenAIImageAdapter()
        self.topaz = topaz if topaz is not None else TopazUpscaleAdapter()
        self.voice = voice if voice is not None else ElevenLabsVoiceAdapter()

    @traced("adapter.creator_real.build_creator", run_type="chain", step=3, provider="creator_real")
    async def build_creator(self, index: int, system_prompt: Optional[str] = None) -> dict[str, Any]:
        """Constrói o creator reutilizável combinando imagem, upscale e voz.

        Retorna o mesmo shape que ``MockAdapter.build_creator``.
        """
        face = await self.image.generate_face(index, system_prompt=system_prompt)
        upscaled = await self.topaz.upscale(face["primary"])
        voice_id = await self.voice.create_voice(index)

        return {
            "id": f"creator-{index}",
            "angles": face["angles"],
            "upscaled_base": upscaled,
            "voice_id": voice_id,
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
    """Fábrica que monta RealCreatorAdapter usando Replicate para upscale e voz.

    - OpenAI Image: roteado pelo Vercel Gateway (AI_GATEWAY_API_KEY).
    - Upscale: Replicate nightmareai/real-esrgan (REPLICATE_API_TOKEN).
    - Voice: Replicate suno-ai/bark (REPLICATE_API_TOKEN).

    Usa um ``replicate.Client`` com timeout generoso — o rosto do GPT Image 2 vem
    como data URI base64 (~2.7MB) e é enviado inline; com cold start do modelo, o
    timeout padrão do client estoura (ReadTimeout).
    """
    rep_client = replicate.Client(
        api_token=os.environ.get("REPLICATE_API_TOKEN"),
        timeout=httpx.Timeout(600.0, connect=15.0),
    )
    return RealCreatorAdapter(
        image=build_openai_image_vercel_adapter(pipeline),
        topaz=ReplicateUpscaleAdapter(runner=rep_client.async_run),
        voice=ReplicateVoiceAdapter(runner=rep_client.async_run),
    )
