"""ReplicateVideoAdapter — vídeo real via SDK oficial ``replicate``.

Usa ``replicate.async_run(ref, input=...)`` para deixar o SDK resolver versionamento,
criação da prediction e polling. LTX 2.3 Fast e PrunaAI P-Video têm contratos de
input explícitos; modelos ainda não plugados caem em mock se chamados explicitamente.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import replicate

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.adapters._throttle import AsyncThrottle
from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, Item
from orchestrator.tracing import traced

Runner = Callable[..., Awaitable[Any]]

_VIDEO_OUTPUT_KEYS = ("video", "video_url", "output")
_LTX_MODEL = "lightricks/ltx-2.3-fast"
_PRUNA_P_VIDEO_MODEL = "prunaai/p-video"
_SUPPORTED_MODELS = frozenset({_LTX_MODEL, _PRUNA_P_VIDEO_MODEL})


def _first_uri(value: Any, empty_msg: str) -> str:
    """Coage um valor do SDK (escalar/FileOutput ou lista deles) para uma URI.

    Lista vazia é erro (``empty_msg``): indexar ``value[0]`` estouraria, e coagir
    ``[]`` para ``str`` produziria ``"[]"`` — uma URI-lixo que só falharia no QC.
    """
    if isinstance(value, list):
        if not value:
            raise RuntimeError(empty_msg)
        value = value[0]
    return str(value)


class ReplicateVideoAdapter:
    """Implementa VideoPort para modelos de vídeo com contrato conhecido no Replicate."""

    def __init__(
        self,
        tiers: list[dict[str, Any]],
        runner: Optional[Runner] = None,
        clip: Optional[dict[str, Any]] = None,
        assembly: Optional[dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        throttle: Optional[AsyncThrottle] = None,
        allow_mock_fallback: bool = True,
    ) -> None:
        self.tiers: dict[str, dict[str, Any]] = {t["name"]: t for t in tiers}
        self._runner: Runner = runner or replicate.async_run
        self._throttle = throttle
        self._mock = MockAdapter(tiers=tiers)
        clip = clip or {}
        self.resolution = str(clip.get("resolution", "1080p"))
        self.aspect_ratio = str(clip.get("aspect_ratio", "9:16"))
        self.fps = int(clip.get("fps", 25))
        self.camera_motion = str(clip.get("camera_motion", "static"))
        self.clip_draft = bool(clip.get("draft", False))
        assembly = assembly or {}
        self.assembly_model = str(assembly.get("model", _PRUNA_P_VIDEO_MODEL))
        self.assembly_duration = int(assembly.get("duration_seconds", 8))
        self.assembly_resolution = str(assembly.get("resolution", "1080p"))
        self.assembly_aspect_ratio = str(assembly.get("aspect_ratio", "9:16"))
        self.assembly_fps = int(assembly.get("fps", 24))
        self.assembly_draft = bool(assembly.get("draft", False))
        self.assembly_generate_audio = bool(assembly.get("generate_audio", False))
        self.assembly_cost_per_second = float(
            assembly.get(
                "cost_per_second",
                next(
                    (
                        tier.get("cost_per_second", 0.0)
                        for tier in tiers
                        if tier.get("model") == self.assembly_model
                    ),
                    0.0,
                ),
            )
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.allow_mock_fallback = allow_mock_fallback

    @traced("adapter.replicate_video.generate_clip", run_type="tool", step="video", provider="replicate")
    async def generate_clip(
        self,
        item_id: str,
        tier: str,
        seconds: int,
        attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact:
        """Gera um clip silencioso ou delega modelos ainda não plugados ao mock."""
        spec = self.tiers[tier]  # KeyError em tier desconhecido (contratual)
        model = spec["model"]
        if model not in _SUPPORTED_MODELS:
            if not self.allow_mock_fallback:
                raise RuntimeError(
                    "Replicate video mock fallback disabled for "
                    f"tier={tier!r}; configure a real model adapter before live run"
                )
            artifact = await self._mock.generate_clip(
                item_id,
                tier,
                seconds,
                attempt,
                system_prompt=system_prompt,
                reference_image_uri=reference_image_uri,
            )
            meta = dict(artifact.meta)
            meta["provider"] = "mock"
            meta["fallback_reason"] = "replicate_model_not_configured"
            return artifact.model_copy(update={"meta": meta})

        prompt = system_prompt or f"Generate a silent vertical UGC video for item {item_id}."
        cost_usd = round(spec["cost_per_second"] * seconds, 4)
        if model == _PRUNA_P_VIDEO_MODEL:
            inp: dict[str, Any] = {
                "prompt": prompt,
                "duration": seconds,
                "resolution": self.resolution,
                "aspect_ratio": self.aspect_ratio,
                "fps": self.fps,
                "draft": self.clip_draft,
                "save_audio": False,
                "prompt_upsampling": False,
            }
        else:
            inp = {
                "prompt": prompt,
                "duration": seconds,
                "generate_audio": False,
                "resolution": self.resolution,
                "aspect_ratio": self.aspect_ratio,
                "fps": self.fps,
                "camera_motion": self.camera_motion,
            }
        if reference_image_uri:
            inp["image"] = reference_image_uri

        output = await with_transport_retry(
            lambda: self._throttled_run(model, input=inp),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.video",
        )
        uri = self._coerce_output(output)

        return Artifact(
            kind="clip",
            uri=uri,
            meta={
                "tier": tier,
                "model": model,
                "seconds": seconds,
                "cost_usd": cost_usd,
                "attempt": attempt,
                "provider": "replicate",
                "generate_audio": False,
                "has_reference_image": bool(reference_image_uri),
            },
        )

    @traced(
        "adapter.replicate_video.assemble",
        run_type="tool",
        step=8,
        provider="replicate",
    )
    async def assemble(
        self,
        item: Item,
        platform: str,
        system_prompt: Optional[str] = None,
    ) -> Artifact:
        """Gera o vídeo final PrunaAI a partir do briefing e imagem do creator."""
        if self.assembly_model != _PRUNA_P_VIDEO_MODEL:
            raise RuntimeError(
                "Replicate assembly requires model "
                f"{_PRUNA_P_VIDEO_MODEL!r}; got {self.assembly_model!r}"
            )
        prompt = system_prompt or (
            f"Create one polished final vertical UGC ad for {platform}. "
            f"Script: {item.script or ''}"
        )
        image: Any = item.creator_image_uri
        if not image and item.creator_image_local_path:
            image = Path(item.creator_image_local_path)
        inp: dict[str, Any] = {
            "prompt": prompt,
            "duration": self.assembly_duration,
            "resolution": self.assembly_resolution,
            "aspect_ratio": self.assembly_aspect_ratio,
            "fps": self.assembly_fps,
            "draft": self.assembly_draft,
            "save_audio": self.assembly_generate_audio,
            "prompt_upsampling": False,
        }
        if image:
            inp["image"] = image

        output = await with_transport_retry(
            lambda: self._throttled_run(self.assembly_model, input=inp),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.assembly",
        )
        uri = self._coerce_output(output)
        return Artifact(
            kind="video",
            uri=uri,
            meta={
                "provider": "replicate",
                "model": self.assembly_model,
                "platform": platform,
                "duration": self.assembly_duration,
                "aspect_ratio": self.assembly_aspect_ratio,
                "resolution": self.assembly_resolution,
                "generate_audio": self.assembly_generate_audio,
                "draft": self.assembly_draft,
                "cost_usd": round(
                    self.assembly_cost_per_second * self.assembly_duration, 4
                ),
                "source_clips": len(item.clips or []),
                "has_reference_image": image is not None,
            },
        )

    async def _throttled_run(self, ref: str, **kwargs: Any) -> Any:
        """Passa cada tentativa pelo throttle global (quando configurado)."""
        if self._throttle is None:
            return await self._runner(ref, **kwargs)
        return await self._throttle.run(lambda: self._runner(ref, **kwargs))

    @staticmethod
    def _coerce_output(output: Any) -> str:
        """Normaliza outputs comuns do SDK para uma URI de vídeo.

        Output nulo/vazio é erro: coagir para ``str`` produziria a URI literal
        ``"None"``, que segue adiante como clip válido e só estoura no QC.
        """
        if output is None:
            raise RuntimeError("Replicate video output is empty")
        if isinstance(output, list):
            return _first_uri(output, "Replicate video output list is empty")
        if isinstance(output, dict):
            if not output:
                raise RuntimeError("Replicate video output dict is empty")
            for key in _VIDEO_OUTPUT_KEYS:
                if output.get(key):
                    return _first_uri(output[key], f"Replicate video output key {key!r} is empty")
            first = next(iter(output.values()))
            return _first_uri(first, "Replicate video output fallback list is empty")
        uri = str(output).strip()
        if not uri:
            raise RuntimeError("Replicate video output is empty")
        return uri
