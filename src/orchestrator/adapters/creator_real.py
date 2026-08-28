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
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
from orchestrator.adapters.voice_factory import build_voice_adapter
from orchestrator.tracing import traced

_log = logging.getLogger(__name__)


class RealCreatorAdapter:
    """

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
        voice: Optional[VoicePort] = None,
        topaz: Optional[Any] = None,
    ) -> None:
        self.image = image if image is not None else OpenAIImageAdapter()
        self.voice = voice if voice is not None else ElevenLabsVoiceAdapter()
        # ``topaz`` foi o upscaler da IMAGEM; o upscale passou para o vídeo final
        # (papel ``upscale`` / ``node_upscale``). Mantido só por compatibilidade de
        # assinatura e NÃO é usado — a face crua vira o ``upscaled_base`` do creator.
        self.topaz = topaz

    @traced("adapter.creator_real.build_creator", run_type="chain", step=3, provider="creator_real")
    async def build_creator(
        self,
        index: int,
        system_prompt: Optional[str] = None,
        voice_profile: Optional[VoiceProfile] = None,
    ) -> dict[str, Any]:
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
        # A face gerada é usada CRUA como base do creator: não upscalamos a imagem.
        # (Além de barato, uma face menos fotorrealista reduz rejeições de conteúdo
        # tipo "may contain real person" no gerador de vídeo.) O upscale acontece
        # depois, sobre o vídeo final montado (papel ``upscale`` / ``node_upscale``).
        primary = face["primary"]

        try:
            create_voice_fn = getattr(self.voice, "create_voice", None)
            if callable(create_voice_fn):
                voice_id = await create_voice_fn(index, voice_profile=resolved_voice)
            else:
                voice_id = ""
        except Exception as exc:  # noqa: BLE001 — voz é opcional; imagem preservada
            _log.error("voz falhou (creator-%d): %s", index, exc)
            voice_id = ""
        resolve_voice_ref = getattr(self.voice, "resolve_voice_ref", None)
        voice_model_ref = (
            resolve_voice_ref(index, resolved_voice)
            if callable(resolve_voice_ref)
            else voice_id
        )

        creator = {
            "id": f"creator-{index}",
            "angles": face["angles"],
            "upscaled_base": primary,
            "voice_id": voice_id,
            "voice_model_ref": voice_model_ref,
            "voice_ref": voice_model_ref,
            "voice_preview_uri": voice_id,
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
        """Gera uma voz NOVA para o creator, preservando a imagem.

        O índice efetivo é ``index + reroll_count``: no pool de vozes do
        ``ReplicateVoiceAdapter`` (seleção por ``index % len(pool)``) isso avança
        para a próxima voz do gênero a cada reroll, sem repetir enquanto o pool
        comportar. ``voice_source_uri``/``voice_preview_uri`` são zerados para o
        caller re-persistir o áudio novo.
        """
        create_voice_fn = getattr(self.voice, "create_voice", None)
        voice_id = (
            await create_voice_fn(index + reroll_count, voice_profile=voice_profile)
            if callable(create_voice_fn)
            else ""
        )
        resolve_voice_ref = getattr(self.voice, "resolve_voice_ref", None)
        voice_model_ref = (
            resolve_voice_ref(index + reroll_count, voice_profile)
            if callable(resolve_voice_ref)
            else voice_id
        )
        return {
            "voice_id": voice_id,
            "voice_ref": voice_model_ref,
            "voice": voice_model_ref,
            "voice_model_ref": voice_model_ref,
            "voice_source_uri": None,
            "voice_preview_uri": None,
        }

    async def design_voice_candidates(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self.voice, "design_voice_candidates"):
            return await self.voice.design_voice_candidates(*args, **kwargs)
        raise AttributeError("self.voice has no design_voice_candidates")

    async def finalize_voice(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self.voice, "finalize_voice"):
            return await self.voice.finalize_voice(*args, **kwargs)
        raise AttributeError("self.voice has no finalize_voice")

    async def reconcile_voice(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self.voice, "reconcile_voice"):
            return await self.voice.reconcile_voice(*args, **kwargs)
        raise AttributeError("self.voice has no reconcile_voice")

    @traced(
        "adapter.creator_real.synthesize_voiceover",
        run_type="tool",
        step="voiceover",
        provider="creator_real",
    )
    async def synthesize_voiceover(
        self,
        *,
        voice_ref: str,
        text: str,
    ) -> Any:
        synthesize = getattr(self.voice, "synthesize_voiceover", None)
        if synthesize is None:
            raise RuntimeError("configured voice adapter does not support full voiceover")
        return await synthesize(voice_ref=voice_ref, text=text)


def build_real_creator_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:
    """Fábrica que monta o RealCreatorAdapter lendo tokens do ambiente.

    Tokens vêm de variáveis de ambiente: ``OPENAI_API_KEY``, ``ELEVENLABS_API_KEY``.
    A imagem NÃO é upscalada — o upscale vive no papel ``upscale`` (vídeo final).
    """
    return RealCreatorAdapter(
        image=OpenAIImageAdapter(),
        voice=build_voice_adapter(pipeline),
    )


def build_real_creator_vercel_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:

    return RealCreatorAdapter(
        image=build_openai_image_vercel_adapter(pipeline),
        voice=build_voice_adapter(pipeline),
    )


def build_real_creator_replicate_adapter(pipeline: dict[str, Any]) -> RealCreatorAdapter:

    rep_client = replicate.Client(
        api_token=os.environ.get("REPLICATE_API_TOKEN"),
        timeout=httpx.Timeout(600.0, connect=15.0),
    )
    # Throttle global: a voz divide o orçamento de rate limit da conta com o adapter
    # de vídeo (contas com crédito baixo têm burst 1).
    throttle = get_replicate_throttle()
    return RealCreatorAdapter(
        image=build_openai_image_vercel_adapter(pipeline),
        voice=ReplicateVoiceAdapter(runner=rep_client.async_run, throttle=throttle),
    )
