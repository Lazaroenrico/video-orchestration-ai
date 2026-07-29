"""VideoPort live via Vercel AI Gateway (Kling + Seedance)."""
from __future__ import annotations

import base64
from typing import Any

import pytest

from orchestrator.adapters.vercel_gateway_video import (
    VercelGatewayVideoAdapter,
    build_vercel_gateway_video_adapter,
)
from orchestrator.adapters import vercel_gateway_video as gateway_video


TIERS = [
    {
        "name": "kling",
        "model": "klingai/kling-v3.0-i2v",
        "cost_per_second": 0.168,
        "max_concurrency": 4,
    },
    {
        "name": "seedance",
        "model": "bytedance/seedance-2.0",
        "cost_per_second": 0.168,
        "max_concurrency": 2,
    },
]


async def test_kling_clip_uses_gateway_model_and_reference_image():
    calls: list[dict[str, Any]] = []

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"kling-mp4"

    adapter = VercelGatewayVideoAdapter(
        tiers=TIERS,
        clip={
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "generate_audio": False,
            "timeout_ms": 600_000,
        },
        runner=fake_runner,
    )

    artifact = await adapter.generate_clip(
        item_id="item-1",
        tier="kling",
        seconds=8,
        attempt=1,
        system_prompt="Silent creator talking to camera.",
        reference_image_uri="data:image/png;base64,QUJD",
    )

    assert calls == [
        {
            "model": "klingai/kling-v3.0-i2v",
            "promptText": "Silent creator talking to camera.",
            "image": {"kind": "data_uri", "uri": "data:image/png;base64,QUJD"},
            "duration": 8,
            "aspectRatio": "9:16",
            "resolution": "1080p",
            "generateAudio": False,
            "timeoutMs": 600_000,
        }
    ]
    assert artifact.uri == "data:video/mp4;base64," + base64.b64encode(b"kling-mp4").decode()
    assert artifact.meta == {
        "tier": "kling",
        "model": "klingai/kling-v3.0-i2v",
        "seconds": 8,
        "cost_usd": 1.344,
        "attempt": 1,
        "provider": "vercel_ai_gateway",
        "generate_audio": False,
        "has_reference_image": True,
    }


async def test_seedance_clip_uses_selected_tier_without_reference_image():
    calls: list[dict[str, Any]] = []

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        calls.append(payload)
        return b"seedance-mp4"

    adapter = VercelGatewayVideoAdapter(tiers=TIERS, runner=fake_runner)

    artifact = await adapter.generate_clip(
        item_id="item-2",
        tier="seedance",
        seconds=5,
        attempt=0,
    )

    assert calls[0]["model"] == "bytedance/seedance-2.0"
    assert "image" not in calls[0]
    assert calls[0]["promptText"] == "Generate a silent vertical UGC video for item item-2."
    assert artifact.meta["cost_usd"] == 0.84
    assert artifact.meta["has_reference_image"] is False


async def test_unknown_gateway_video_tier_is_rejected_before_calling_provider():
    async def unused_runner(payload: dict[str, Any]) -> bytes:
        raise AssertionError(f"provider should not be called: {payload}")

    adapter = VercelGatewayVideoAdapter(tiers=TIERS, runner=unused_runner)

    with pytest.raises(KeyError):
        await adapter.generate_clip(
            item_id="item-3",
            tier="ltx",
            seconds=8,
            attempt=0,
        )


async def test_gateway_video_removes_temporary_reference_after_generation(
    monkeypatch,
    tmp_path,
):
    temporary = tmp_path / "reference.png"
    temporary.write_bytes(b"image")

    async def fake_prepare(
        uri: str,
        *,
        cleanup_paths: list,
        max_bytes: int,
    ) -> dict[str, str]:
        assert uri == "https://cdn.example/creator.png"
        assert max_bytes == gateway_video.KLING_IMAGE_TARGET_BYTES
        cleanup_paths.append(temporary)
        return {"kind": "path", "path": str(temporary)}

    async def fake_runner(payload: dict[str, Any]) -> bytes:
        assert temporary.exists()
        return b"video"

    monkeypatch.setattr(
        gateway_video,
        "_prepare_reference_image_payload",
        fake_prepare,
    )
    adapter = VercelGatewayVideoAdapter(tiers=TIERS, runner=fake_runner)

    await adapter.generate_clip(
        item_id="item-4",
        tier="kling",
        seconds=8,
        attempt=0,
        reference_image_uri="https://cdn.example/creator.png",
    )

    assert not temporary.exists()


def test_gateway_video_factory_reads_clip_configuration():
    adapter = build_vercel_gateway_video_adapter(
        {
            "tiers": TIERS,
            "clip": {
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "generate_audio": False,
                "timeout_ms": 123_000,
            },
        }
    )

    assert adapter.resolution == "720p"
    assert adapter.aspect_ratio == "16:9"
    assert adapter.generate_audio is False
    assert adapter.timeout_ms == 123_000
