"""ReplicateVideoAdapter — vídeo real via SDK oficial ``replicate``.

O caminho live expõe criação, consulta e cancelamento da prediction separadamente.
Isso permite persistir o ``prediction_id`` antes do polling e reconciliar respostas
ambíguas sem repetir uma criação potencialmente cobrada.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import replicate

from orchestrator.adapters._retry import with_idempotent_retry, with_transport_retry
from orchestrator.adapters._throttle import AsyncThrottle
from orchestrator.adapters.mock import MockAdapter
from orchestrator.graph.state import Artifact, Item
from orchestrator.tracing import traced

Runner = Callable[..., Awaitable[Any]]

_VIDEO_OUTPUT_KEYS = ("video", "video_url", "output")
_LTX_MODEL = "lightricks/ltx-2.3-fast"
_PRUNA_P_VIDEO_MODEL = "prunaai/p-video"
_SUPPORTED_MODELS = frozenset({_LTX_MODEL, _PRUNA_P_VIDEO_MODEL})
_TERMINAL_PREDICTION_STATUSES = frozenset({"succeeded", "failed", "canceled"})


@dataclass(frozen=True)
class VideoPrediction:
    """Provider-neutral snapshot of a Replicate prediction."""

    id: str
    status: str
    output: Any = None
    error: str | None = None


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
        prediction_client: Any | None = None,
        clip: Optional[dict[str, Any]] = None,
        assembly: Optional[dict[str, Any]] = None,
        latentsync: Optional[dict[str, Any]] = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        throttle: Optional[AsyncThrottle] = None,
        allow_mock_fallback: bool = True,
    ) -> None:
        self.tiers: dict[str, dict[str, Any]] = {t["name"]: t for t in tiers}
        # ``runner`` remains an injection seam for legacy/offline adapter tests.
        # Live construction uses the explicit prediction client below.
        self._runner = runner
        self._assembly_runner: Runner = runner or replicate.async_run
        self._prediction_client = prediction_client or replicate.default_client
        self._throttle = throttle
        self._mock = MockAdapter(tiers=tiers)
        clip = clip or {}
        self.resolution = str(clip.get("resolution", "1080p"))
        self.aspect_ratio = str(clip.get("aspect_ratio", "9:16"))
        self.fps = int(clip.get("fps", 25))
        self.camera_motion = str(clip.get("camera_motion", "static"))
        self.clip_draft = bool(clip.get("draft", False))
        self.clip_timeout_seconds = max(float(clip.get("timeout_ms", 900_000)) / 1000, 0.001)
        self.poll_interval_seconds = max(float(clip.get("poll_interval_seconds", 1.0)), 0.0)
        latentsync = latentsync or {}
        self.latentsync_enabled = bool(latentsync.get("enabled", True))
        self.latentsync_model = str(latentsync.get("model", "bytedance/latentsync"))
        self.latentsync_resolution = str(latentsync.get("resolution", "720p"))
        self.latentsync_max_retries = int(latentsync.get("max_retries", 3))
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
        audio_uri: Optional[str] = None,
    ) -> Artifact:
        """Gera um clip silencioso e aplica LatentSync quando o áudio é fornecido."""
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
                audio_uri=audio_uri,
            )
            meta = dict(artifact.meta)
            meta["provider"] = "mock"
            meta["fallback_reason"] = "replicate_model_not_configured"
            return artifact.model_copy(update={"meta": meta})

        model, inp, cost_usd = self._clip_input(
            item_id=item_id,
            tier=tier,
            seconds=seconds,
            system_prompt=system_prompt,
            reference_image_uri=reference_image_uri,
        )
        if self._runner is None:
            prediction = await self.submit_clip_prediction(
                item_id=item_id,
                tier=tier,
                seconds=seconds,
                attempt=attempt,
                system_prompt=system_prompt,
                reference_image_uri=reference_image_uri,
            )
            async with asyncio.timeout(self.clip_timeout_seconds):
                while prediction.status not in _TERMINAL_PREDICTION_STATUSES:
                    await asyncio.sleep(self.poll_interval_seconds)
                    prediction = await self.get_video_prediction(prediction.id)
            return self.clip_artifact_from_prediction(
                prediction,
                tier=tier,
                seconds=seconds,
                attempt=attempt,
                reference_image_uri=reference_image_uri,
            )

        output = await with_transport_retry(
            lambda: self._throttled_run(model, input=inp),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.video",
        )
        uri = self._coerce_output(output)

        meta = {
            "tier": tier,
            "model": model,
            "seconds": seconds,
            "cost_usd": cost_usd,
            "attempt": attempt,
            "provider": "replicate",
            "generate_audio": False,
            "has_reference_image": bool(reference_image_uri),
        }

        if audio_uri and self.latentsync_enabled:
            latentsync_inp = {
                "video": uri,
                "audio": audio_uri,
                "resolution": self.latentsync_resolution,
            }
            latentsync_output = await with_idempotent_retry(
                lambda: self._throttled_run(self.latentsync_model, input=latentsync_inp),
                max_retries=self.latentsync_max_retries,
                backoff_base=self.backoff_base,
                label="replicate.latentsync",
            )
            uri = self._coerce_output(latentsync_output)
            meta["latentsync_applied"] = True
            meta["latentsync_model"] = self.latentsync_model

        return Artifact(
            kind="clip",
            uri=uri,
            meta=meta,
        )

    def clip_model(self, tier: str) -> str:
        """Return the configured model without exposing the prompt/input payload."""
        return str(self.tiers[tier]["model"])

    def _clip_input(
        self,
        *,
        item_id: str,
        tier: str,
        seconds: int,
        system_prompt: Optional[str],
        reference_image_uri: Optional[str],
    ) -> tuple[str, dict[str, Any], float]:
        spec = self.tiers[tier]
        model = str(spec["model"])
        if model not in _SUPPORTED_MODELS:
            raise RuntimeError(f"Replicate prediction lifecycle does not support model {model!r}")
        prompt = system_prompt or f"Generate a silent vertical UGC video for item {item_id}."
        cost_usd = round(float(spec["cost_per_second"]) * seconds, 4)
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
        return model, inp, cost_usd

    async def submit_clip_prediction(
        self,
        *,
        item_id: str,
        tier: str,
        seconds: int,
        attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> VideoPrediction:
        """Create once, retrying only failures proven to happen before sending."""
        del attempt  # belongs to effect identity/artifact metadata, not provider input
        model, inp, _cost = self._clip_input(
            item_id=item_id,
            tier=tier,
            seconds=seconds,
            system_prompt=system_prompt,
            reference_image_uri=reference_image_uri,
        )
        params: dict[str, Any] = {"wait": False}
        if webhook_url:
            params.update(
                webhook=webhook_url,
                webhook_events_filter=["start", "completed"],
            )
        prediction = await with_transport_retry(
            lambda: self._throttled_prediction_call(
                lambda: self._prediction_client.models.predictions.async_create(
                    model=model,
                    input=inp,
                    **params,
                )
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.video.create",
        )
        return self._snapshot(prediction)

    async def get_video_prediction(self, prediction_id: str) -> VideoPrediction:
        prediction = await with_idempotent_retry(
            lambda: self._throttled_prediction_call(
                lambda: self._prediction_client.predictions.async_get(prediction_id)
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.video.get",
        )
        return self._snapshot(prediction)

    async def cancel_video_prediction(self, prediction_id: str) -> VideoPrediction:
        prediction = await with_idempotent_retry(
            lambda: self._throttled_prediction_call(
                lambda: self._prediction_client.predictions.async_cancel(prediction_id)
            ),
            max_retries=self.max_retries,
            backoff_base=self.backoff_base,
            label="replicate.video.cancel",
        )
        return self._snapshot(prediction)

    def clip_artifact_from_prediction(
        self,
        prediction: VideoPrediction,
        *,
        tier: str,
        seconds: int,
        attempt: int,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact:
        if prediction.status != "succeeded":
            detail = prediction.error or prediction.status
            raise RuntimeError(f"Replicate video prediction did not succeed: {detail}")
        spec = self.tiers[tier]
        uri = self._coerce_output(prediction.output)
        return Artifact(
            kind="clip",
            uri=uri,
            meta={
                "tier": tier,
                "model": spec["model"],
                "seconds": seconds,
                "cost_usd": round(float(spec["cost_per_second"]) * seconds, 4),
                "attempt": attempt,
                "provider": "replicate",
                "prediction_id": prediction.id,
                "generate_audio": False,
                "has_reference_image": bool(reference_image_uri),
            },
        )

    @staticmethod
    def _snapshot(prediction: Any) -> VideoPrediction:
        prediction_id = str(getattr(prediction, "id", "") or "").strip()
        status = str(getattr(prediction, "status", "") or "").strip().lower()
        if not prediction_id or not status:
            raise RuntimeError("Replicate returned a prediction without id/status")
        error = getattr(prediction, "error", None)
        return VideoPrediction(
            id=prediction_id,
            status=status,
            output=getattr(prediction, "output", None),
            error=str(error) if error else None,
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
        runner = self._runner or self._assembly_runner
        if self._throttle is None:
            return await runner(ref, **kwargs)
        return await self._throttle.run(lambda: runner(ref, **kwargs))

    async def _throttled_prediction_call(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._throttle is None:
            return await operation()
        return await self._throttle.run(operation)

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
