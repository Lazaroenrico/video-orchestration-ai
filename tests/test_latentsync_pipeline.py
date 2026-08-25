"""Testes do pipeline de Talking Head com LatentSync (LTX + ElevenLabs -> LatentSync 720p)."""
from __future__ import annotations

from typing import Any

import pytest
from replicate.exceptions import ReplicateError

from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.graph.state import Artifact

TIERS = [
    {
        "name": "ltx",
        "model": "lightricks/ltx-2.3-fast",
        "cost_per_second": 0.01,
        "max_concurrency": 16,
    },
]

LATENTSYNC_CONFIG = {
    "enabled": True,
    "model": "bytedance/latentsync",
    "resolution": "720p",
    "max_retries": 3,
    "required": True,
    "cost_per_second": 0.003,
}


async def test_generate_clip_executes_ltx_then_latentsync_when_audio_provided():
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        if ref == "lightricks/ltx-2.3-fast":
            return "https://cdn.replicate.com/ltx_raw.mp4"
        if ref == "bytedance/latentsync":
            return "https://cdn.replicate.com/latentsync_final.mp4"
        return "https://cdn.replicate.com/unknown.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
    )

    artifact = await adapter.generate_clip(
        item_id="item-talking-head",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Creator talks enthusiastically about the product.",
        reference_image_uri="data:image/png;base64,creator_img",
        audio_uri="https://cdn.r2.com/elevenlabs_narration.wav",
    )

    assert isinstance(artifact, Artifact)
    assert artifact.kind == "clip"
    assert artifact.uri == "https://cdn.replicate.com/latentsync_final.mp4"
    assert len(calls) == 2

    # Chamada 1: LTX 720p
    assert calls[0]["ref"] == "lightricks/ltx-2.3-fast"
    assert calls[0]["input"]["resolution"] == "720p"
    assert calls[0]["input"]["image"] == "data:image/png;base64,creator_img"

    # Chamada 2: LatentSync
    assert calls[1]["ref"] == "bytedance/latentsync"
    assert calls[1]["input"]["video"] == "https://cdn.replicate.com/ltx_raw.mp4"
    assert calls[1]["input"]["audio"] == "https://cdn.r2.com/elevenlabs_narration.wav"

    assert artifact.meta["latentsync_applied"] is True
    assert artifact.meta["latentsync_model"] == "bytedance/latentsync"


async def test_latentsync_retries_3_times_and_raises_error_if_all_fail():
    calls: list[dict[str, Any]] = []

    async def fake_runner_failing_latentsync(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        if ref == "lightricks/ltx-2.3-fast":
            return "https://cdn.replicate.com/ltx_raw.mp4"
        if ref == "bytedance/latentsync":
            raise ReplicateError(status=503, detail="GPU Busy: Service Unavailable")
        return "https://cdn.replicate.com/unknown.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner_failing_latentsync,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
        backoff_base=0.001,
    )

    # Não deve fazer fallback para o vídeo silencioso. Deve lançar exceção após 3 tentativas.
    with pytest.raises(ReplicateError):
        await adapter.generate_clip(
            item_id="item-talking-head",
            tier="ltx",
            seconds=8,
            attempt=1,
            system_prompt="Creator talks enthusiastically about the product.",
            reference_image_uri="data:image/png;base64,creator_img",
            audio_uri="https://cdn.r2.com/elevenlabs_narration.wav",
        )

    # 1 chamada LTX + 4 chamadas LatentSync (1ª tentativa + 3 retries)
    latentsync_calls = [c for c in calls if c["ref"] == "bytedance/latentsync"]
    assert len(latentsync_calls) == 4


async def test_latentsync_mock_adapter_metadata():
    adapter = MockAdapter(tiers=TIERS)
    artifact = await adapter.generate_clip(
        item_id="item-mock-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Creator talks enthusiastically.",
        reference_image_uri="data:image/png;base64,creator_img",
        audio_uri="https://cdn.r2.com/audio.wav",
    )
    assert artifact.meta.get("latentsync_applied") is True


async def test_generate_clip_with_prediction_client_chains_latentsync():
    from types import SimpleNamespace

    created: list[dict[str, Any]] = []

    async def async_create(*, model=None, version=None, input=None, **params):
        created.append({"model": model, "version": version, "input": input, "params": params})
        if model == "lightricks/ltx-2.3-fast":
            return SimpleNamespace(id="pred-ltx-10", status="succeeded", output="https://cdn.replicate.com/ltx10.mp4", error=None)
        if model == "bytedance/latentsync" or version == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293":
            return SimpleNamespace(id="pred-ls-10", status="succeeded", output="https://cdn.replicate.com/ls10.mp4", error=None)
        raise ValueError(f"unknown model/version {model}/{version}")

    async def async_get(prediction_id):
        if prediction_id == "pred-ltx-10":
            return SimpleNamespace(id="pred-ltx-10", status="succeeded", output="https://cdn.replicate.com/ltx10.mp4", error=None)
        if prediction_id == "pred-ls-10":
            return SimpleNamespace(id="pred-ls-10", status="succeeded", output="https://cdn.replicate.com/ls10.mp4", error=None)
        raise ValueError(f"unknown prediction {prediction_id}")

    predictions = SimpleNamespace(
        async_create=async_create,
        async_get=async_get,
        async_cancel=lambda _id: None,
    )

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        prediction_client=SimpleNamespace(
            models=SimpleNamespace(predictions=predictions),
            predictions=predictions,
        ),
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
    )

    artifact = await adapter.generate_clip(
        item_id="item-talking-head",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Creator talks enthusiastically about the product.",
        reference_image_uri="data:image/png;base64,creator_img",
        audio_uri="https://cdn.r2.com/elevenlabs_narration.wav",
    )

    assert artifact.uri == "https://cdn.replicate.com/ls10.mp4"
    assert artifact.meta["latentsync_applied"] is True
    assert artifact.meta["latentsync_model"] == "bytedance/latentsync"
    assert artifact.meta["prediction_id"] == "pred-ls-10"
    assert len(created) == 2
    assert created[0]["model"] == "lightricks/ltx-2.3-fast"
    assert created[1]["version"] == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293"
    assert created[1]["input"]["video"] == "https://cdn.replicate.com/ltx10.mp4"
    assert created[1]["input"]["audio"] == "https://cdn.r2.com/elevenlabs_narration.wav"
    assert artifact.meta["base_clip_uri"] == "https://cdn.replicate.com/ltx10.mp4"


async def test_generate_clip_raises_when_latentsync_required_and_audio_missing():
    async def fake_runner(ref: str, input: dict[str, Any]):
        return "https://cdn.replicate.com/ltx_raw.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
    )

    with pytest.raises(RuntimeError, match="LatentSync is required.*audio_uri"):
        await adapter.generate_clip(
            item_id="item-talking-head",
            tier="ltx",
            seconds=8,
            attempt=1,
            system_prompt="Creator talks enthusiastically about the product.",
            reference_image_uri="data:image/png;base64,creator_img",
            audio_uri=None,
        )


async def test_generate_clip_raises_when_latentsync_required_and_latentsync_disabled():
    async def fake_runner(ref: str, input: dict[str, Any]):
        return "https://cdn.replicate.com/ltx_raw.mp4"

    disabled_config = dict(LATENTSYNC_CONFIG, enabled=False, required=True)
    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=disabled_config,
        allow_mock_fallback=False,
    )

    with pytest.raises(RuntimeError, match="LatentSync is required.*disabled"):
        await adapter.generate_clip(
            item_id="item-talking-head",
            tier="ltx",
            seconds=8,
            attempt=1,
            system_prompt="Creator talks enthusiastically about the product.",
            reference_image_uri="data:image/png;base64,creator_img",
            audio_uri="https://cdn.r2.com/elevenlabs_narration.wav",
        )


async def test_mock_adapter_raises_when_latentsync_required_and_audio_missing():
    adapter = MockAdapter(tiers=TIERS, latentsync={"required": True, "enabled": True})
    with pytest.raises(RuntimeError, match="LatentSync is required.*audio_uri"):
        await adapter.generate_clip(
            item_id="item-mock-1",
            tier="ltx",
            seconds=8,
            attempt=1,
            system_prompt="Creator talks enthusiastically.",
            reference_image_uri="data:image/png;base64,creator_img",
            audio_uri=None,
            stage="talking_head",
        )


async def test_generate_clip_product_demo_succeeds_without_audio_even_when_latentsync_required():
    async def fake_runner(ref: str, input: dict[str, Any]):
        return "https://cdn.replicate.com/demo.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
    )

    # Product demo without audio should succeed and NOT raise
    artifact = await adapter.generate_clip(
        item_id="item-demo-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Show the product bottle.",
        reference_image_uri="data:image/png;base64,product_img",
        audio_uri=None,
        stage="product_demo",
    )
    assert artifact.uri == "https://cdn.replicate.com/demo.mp4"
    assert artifact.meta.get("latentsync_applied") is None

    mock_adapter = MockAdapter(tiers=TIERS, latentsync=LATENTSYNC_CONFIG)
    mock_artifact = await mock_adapter.generate_clip(
        item_id="item-demo-2",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Show the product bottle.",
        reference_image_uri="data:image/png;base64,product_img",
        audio_uri=None,
        stage="product_demo",
    )
    assert mock_artifact.kind == "clip"
    assert mock_artifact.meta.get("latentsync_applied") is None


async def test_latentsync_cost_is_included_in_clip_cost():
    async def fake_runner(ref: str, input: dict[str, Any]):
        if ref == "lightricks/ltx-2.3-fast":
            return "https://cdn.replicate.com/ltx_raw.mp4"
        return "https://cdn.replicate.com/latentsync_final.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        clip={"resolution": "720p", "aspect_ratio": "9:16", "fps": 24},
        latentsync=LATENTSYNC_CONFIG,
        allow_mock_fallback=False,
    )

    artifact = await adapter.generate_clip(
        item_id="item-cost-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="Creator talks.",
        reference_image_uri="data:image/png;base64,img",
        audio_uri="https://cdn.r2.com/audio.wav",
        stage="talking_head",
    )
    # LTX (0.01 * 8 = 0.08) + LatentSync (0.003 * 8 = 0.024) = 0.104
    assert artifact.meta["cost_usd"] == 0.104
    assert artifact.meta["latentsync_cost_usd"] == 0.024

    mock_adapter = MockAdapter(tiers=TIERS, latentsync=LATENTSYNC_CONFIG)
    mock_art = await mock_adapter.generate_clip(
        item_id="item-cost-mock",
        tier="ltx",
        seconds=8,
        attempt=1,
        audio_uri="https://cdn.r2.com/audio.wav",
        stage="talking_head",
    )
    assert mock_art.meta["cost_usd"] == 0.104
    assert mock_art.meta["latentsync_cost_usd"] == 0.024

