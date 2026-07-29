"""Kling e Seedance via Vercel AI Gateway para os clips intermediários."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

from orchestrator.adapters.vercel_seedance_assembly import (
    BridgeRunner,
    _prepare_reference_image_payload,
    _run_node_bridge,
)
from orchestrator.graph.state import Artifact
from orchestrator.tracing import traced

DEFAULT_RESOLUTION = "1080p"
DEFAULT_ASPECT_RATIO = "9:16"
DEFAULT_TIMEOUT_MS = 900_000
KLING_IMAGE_TARGET_BYTES = 9 * 1024 * 1024


class VercelGatewayVideoAdapter:
    """Implementa VideoPort escolhendo o model id do tier configurado."""

    def __init__(
        self,
        *,
        tiers: list[dict[str, Any]],
        clip: Optional[dict[str, Any]] = None,
        runner: Optional[BridgeRunner] = None,
    ) -> None:
        self.tiers = {str(tier["name"]): tier for tier in tiers}
        clip = clip or {}
        self.resolution = str(clip.get("resolution", DEFAULT_RESOLUTION))
        self.aspect_ratio = str(clip.get("aspect_ratio", DEFAULT_ASPECT_RATIO))
        self.generate_audio = bool(clip.get("generate_audio", False))
        self.timeout_ms = int(clip.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        self._runner = runner or _run_node_bridge

    @traced(
        "adapter.vercel_gateway_video.generate_clip",
        run_type="tool",
        step="video",
        provider="vercel_ai_gateway",
    )
    async def generate_clip(
        self,
        item_id: str,
        tier: str,
        seconds: int,
        attempt: int,
        system_prompt: Optional[str] = None,
        reference_image_uri: Optional[str] = None,
    ) -> Artifact:
        spec = self.tiers[tier]
        model = str(spec["model"])
        prompt = system_prompt or (
            f"Generate a silent vertical UGC video for item {item_id}."
        )
        payload: dict[str, Any] = {
            "model": model,
            "promptText": prompt,
            "duration": seconds,
            "aspectRatio": self.aspect_ratio,
            "resolution": self.resolution,
            "generateAudio": self.generate_audio,
            "timeoutMs": self.timeout_ms,
        }
        cleanup_paths: list[Path] = []
        max_image_bytes = (
            KLING_IMAGE_TARGET_BYTES
            if model.startswith("klingai/")
            else 28 * 1024 * 1024
        )
        image = await _prepare_reference_image_payload(
            reference_image_uri,
            cleanup_paths=cleanup_paths,
            max_bytes=max_image_bytes,
        )
        if image is not None:
            payload["image"] = image

        try:
            data = await self._runner(payload)
        finally:
            for path in cleanup_paths:
                path.unlink(missing_ok=True)

        return Artifact(
            kind="clip",
            uri="data:video/mp4;base64," + base64.b64encode(data).decode(),
            meta={
                "tier": tier,
                "model": model,
                "seconds": seconds,
                "cost_usd": round(float(spec["cost_per_second"]) * seconds, 4),
                "attempt": attempt,
                "provider": "vercel_ai_gateway",
                "generate_audio": self.generate_audio,
                "has_reference_image": image is not None,
            },
        )


def build_vercel_gateway_video_adapter(
    pipeline: dict[str, Any],
) -> VercelGatewayVideoAdapter:
    return VercelGatewayVideoAdapter(
        tiers=pipeline["tiers"],
        clip=pipeline.get("clip", {}),
    )
