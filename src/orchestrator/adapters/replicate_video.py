"""ReplicateVideoAdapter — vídeo real via SDK oficial ``replicate``.

Usa ``replicate.async_run(ref, input=...)`` para deixar o SDK resolver versionamento,
criação da prediction e polling. O tier ``ltx`` usa LTX 2.3 Fast sem áudio; tiers
premium ainda não têm refs reais confirmadas e caem em mock se chamados explicitamente.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import replicate

from orchestrator.adapters._retry import with_transport_retry
from orchestrator.adapters._throttle import AsyncThrottle
from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact
from orchestrator.tracing import traced

Runner = Callable[..., Awaitable[Any]]

_VIDEO_OUTPUT_KEYS = ("video", "video_url", "output")


class ReplicateVideoAdapter:
    """Implementa VideoPort chamando Replicate LTX 2.3 Fast para o tier ``ltx``."""

    def __init__(
        self,
        tiers: list[dict[str, Any]],
        runner: Optional[Runner] = None,
        clip: Optional[dict[str, Any]] = None,
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
        """Gera um clip LTX silencioso ou delega tiers ainda não plugados ao mock."""
        spec = self.tiers[tier]  # KeyError em tier desconhecido (contratual)
        if tier != "ltx":
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

        model = spec["model"]
        prompt = system_prompt or f"Generate a silent vertical UGC video for item {item_id}."
        cost_usd = round(spec["cost_per_second"] * seconds, 4)
        inp: dict[str, Any] = {
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
            if not output:
                raise RuntimeError("Replicate video output list is empty")
            return str(output[0])
        if isinstance(output, dict):
            for key in _VIDEO_OUTPUT_KEYS:
                value = output.get(key)
                if value:
                    if isinstance(value, list):
                        if not value:
                            raise RuntimeError(f"Replicate video output key {key!r} is empty")
                        return str(value[0])
                    return str(value)
            if not output:
                raise RuntimeError("Replicate video output dict is empty")
            first = next(iter(output.values()))
            if isinstance(first, list):
                if not first:
                    raise RuntimeError("Replicate video output fallback list is empty")
                return str(first[0])
            return str(first)
        uri = str(output).strip()
        if not uri:
            raise RuntimeError("Replicate video output is empty")
        return uri
