"""ReplicateVideoAdapter via SDK oficial, sem rede."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.graph.state import Artifact, Item

TIERS = [
    {
        "name": "ltx",
        "model": "lightricks/ltx-2.3-fast",
        "cost_per_second": 0.01,
        "max_concurrency": 16,
    },
    {"name": "kling", "model": "kling-3.0", "cost_per_second": 0.10, "max_concurrency": 6},
    {"name": "seedance", "model": "seedance-2.0", "cost_per_second": 0.168, "max_concurrency": 2},
]


def _mock_clip_generator(tiers, **kwargs):
    # Mesma fiação da composition root (registry._mock_clip_generator): o
    # adapter pago não constrói o mock sozinho — o fallback é injetado aqui.
    return MockAdapter(tiers=tiers, **kwargs).generate_clip


def _make_adapter(output: Any = "https://cdn.replicate.com/clip.mp4", **kwargs: Any):
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        return output

    tiers = kwargs.pop("tiers", TIERS)
    if kwargs.get("allow_mock_fallback", True) and "mock_clip_generator" not in kwargs:
        kwargs["mock_clip_generator"] = _mock_clip_generator(
            tiers, latentsync=kwargs.get("latentsync")
        )
    adapter = ReplicateVideoAdapter(tiers=tiers, runner=fake_runner, **kwargs)
    return adapter, calls


async def test_generate_clip_calls_ltx_model_with_reference_image_and_no_audio():
    adapter, calls = _make_adapter()

    artifact = await adapter.generate_clip(
        "item-abc",
        "ltx",
        8,
        1,
        system_prompt="Creator explains serum benefits.",
        reference_image_uri="data:image/png;base64,abc",
    )

    assert isinstance(artifact, Artifact)
    assert artifact.kind == "clip"
    assert artifact.uri == "https://cdn.replicate.com/clip.mp4"
    assert calls == [
        {
            "ref": "lightricks/ltx-2.3-fast",
            "input": {
                "prompt": "Creator explains serum benefits.",
                "duration": 8,
                "generate_audio": False,
                "resolution": "1080p",
                "aspect_ratio": "9:16",
                "fps": 25,
                "camera_motion": "static",
                "image": "data:image/png;base64,abc",
            },
        }
    ]
    assert artifact.meta["provider"] == "replicate"
    assert artifact.meta["model"] == "lightricks/ltx-2.3-fast"
    assert artifact.meta["tier"] == "ltx"
    assert artifact.meta["seconds"] == 8
    assert artifact.meta["attempt"] == 1
    assert artifact.meta["cost_usd"] == pytest.approx(0.08)
    assert artifact.meta["generate_audio"] is False
    assert artifact.meta["has_reference_image"] is True


async def test_generate_clip_calls_pruna_p_video_with_its_public_input_contract():
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        return "https://cdn.replicate.com/pruna.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=[
            {
                "name": "pruna",
                "model": "prunaai/p-video",
                "cost_per_second": 0.04,
                "max_concurrency": 1,
            }
        ],
        runner=fake_runner,
        clip={"resolution": "1080p", "aspect_ratio": "9:16", "fps": 24},
        allow_mock_fallback=False,
    )

    artifact = await adapter.generate_clip(
        "item-pruna",
        "pruna",
        8,
        0,
        system_prompt="Creator demonstrates the product.",
        reference_image_uri="data:image/png;base64,abc",
    )

    assert calls == [
        {
            "ref": "prunaai/p-video",
            "input": {
                "prompt": "Creator demonstrates the product.",
                "duration": 8,
                "resolution": "1080p",
                "aspect_ratio": "9:16",
                "fps": 24,
                "draft": False,
                "save_audio": False,
                "prompt_upsampling": False,
                "image": "data:image/png;base64,abc",
            },
        }
    ]
    assert artifact.uri == "https://cdn.replicate.com/pruna.mp4"
    assert artifact.meta["provider"] == "replicate"
    assert artifact.meta["model"] == "prunaai/p-video"
    assert artifact.meta["cost_usd"] == pytest.approx(0.32)


async def test_assemble_calls_pruna_p_video_in_low_cost_draft_mode():
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        return "https://cdn.replicate.com/pruna-final.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=[
            {
                "name": "pruna",
                "model": "prunaai/p-video",
                "cost_per_second": 0.01,
                "max_concurrency": 1,
            }
        ],
        runner=fake_runner,
        clip={"resolution": "1080p", "aspect_ratio": "9:16", "fps": 24, "draft": True},
        assembly={
            "model": "prunaai/p-video",
            "duration_seconds": 8,
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "fps": 24,
            "draft": True,
            "generate_audio": False,
            "cost_per_second": 0.01,
        },
        allow_mock_fallback=False,
    )
    item = Item(
        id="item-pruna",
        concept={"id": "item-pruna", "hook": "Demonstrate the product"},
        creator_image_uri="data:image/png;base64,abc",
        script="HOOK: demonstrate the product",
        clips=[
            Artifact(kind="clip", uri="r2://bucket/talking-head.mp4"),
            Artifact(kind="clip", uri="r2://bucket/product-demo.mp4"),
        ],
    )

    artifact = await adapter.assemble(
        item=item,
        platform="tiktok",
        system_prompt="Create the final vertical UGC ad.",
    )

    assert calls == [
        {
            "ref": "prunaai/p-video",
            "input": {
                "prompt": "Create the final vertical UGC ad.",
                "duration": 8,
                "resolution": "1080p",
                "aspect_ratio": "9:16",
                "fps": 24,
                "draft": True,
                "save_audio": False,
                "prompt_upsampling": False,
                "image": "data:image/png;base64,abc",
            },
        }
    ]
    assert artifact.kind == "video"
    assert artifact.uri == "https://cdn.replicate.com/pruna-final.mp4"
    assert artifact.meta == {
        "provider": "replicate",
        "model": "prunaai/p-video",
        "platform": "tiktok",
        "duration": 8,
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "generate_audio": False,
        "draft": True,
        "cost_usd": 0.08,
        "source_clips": 2,
        "has_reference_image": True,
    }


async def test_assemble_rejects_an_unconfigured_model():
    adapter, _ = _make_adapter(
        assembly={"model": "other/video-model"},
        allow_mock_fallback=False,
    )
    item = Item(id="item-abc", concept={"id": "item-abc", "hook": "h"})

    with pytest.raises(RuntimeError, match="requires model.*prunaai/p-video"):
        await adapter.assemble(item=item, platform="tiktok")


async def test_assemble_uses_default_prompt_and_local_creator_image(tmp_path):
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        return "https://cdn.replicate.com/final.mp4"

    image_path = tmp_path / "creator.png"
    image_path.write_bytes(b"png")
    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=fake_runner,
        assembly={"model": "prunaai/p-video"},
    )
    item = Item(
        id="item-abc",
        concept={"id": "item-abc", "hook": "h"},
        script="HOOK: explain the offer",
        creator_image_local_path=str(image_path),
    )

    await adapter.assemble(item=item, platform="instagram")

    assert calls[0]["input"]["image"] == image_path
    assert "final vertical UGC ad for instagram" in calls[0]["input"]["prompt"]
    assert "HOOK: explain the offer" in calls[0]["input"]["prompt"]


async def test_generate_clip_omits_image_when_reference_missing():
    adapter, calls = _make_adapter()

    await adapter.generate_clip("item-abc", "ltx", 8, 1, system_prompt="prompt")

    assert "image" not in calls[0]["input"]


async def test_generate_clip_defaults_prompt_when_system_prompt_missing():
    adapter, calls = _make_adapter()

    await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert calls[0]["input"]["prompt"] == "Generate a silent vertical UGC video for item item-abc."


async def test_generate_clip_normalizes_list_output():
    adapter, _ = _make_adapter(output=["https://cdn.replicate.com/list.mp4"])

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/list.mp4"


async def test_generate_clip_normalizes_dict_output():
    adapter, _ = _make_adapter(output={"video": "https://cdn.replicate.com/dict.mp4"})

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/dict.mp4"


async def test_generate_clip_retries_transport_errors_then_succeeds():
    calls = 0

    async def flaky_runner(ref: str, input: dict[str, Any]):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("temporary")
        return "https://cdn.replicate.com/ok.mp4"

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        runner=flaky_runner,
        max_retries=1,
        backoff_base=0,
    )

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/ok.mp4"
    assert calls == 2


async def test_prediction_get_retries_read_timeout_and_server_error():
    calls = 0

    async def async_get(prediction_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("poll response timed out")
        if calls == 2:
            request = httpx.Request(
                "GET", f"https://api.replicate.com/v1/predictions/{prediction_id}"
            )
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("unavailable", request=request, response=response)
        return SimpleNamespace(
            id=prediction_id,
            status="succeeded",
            output="https://cdn.replicate.com/polled.mp4",
            error=None,
        )

    predictions = SimpleNamespace(
        async_get=async_get,
        async_create=None,
        async_cancel=None,
    )
    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        prediction_client=SimpleNamespace(predictions=predictions),
        max_retries=2,
        backoff_base=0,
    )

    prediction = await adapter.get_video_prediction("prediction-1")

    assert prediction.status == "succeeded"
    assert calls == 3


async def test_unsupported_models_fallback_to_mock_clip():
    adapter, calls = _make_adapter()

    artifact = await adapter.generate_clip("item-abc", "kling", 8, 1)

    assert calls == []
    assert artifact.kind == "clip"
    assert artifact.meta["provider"] == "mock"
    assert artifact.meta["fallback_reason"] == "replicate_model_not_configured"
    assert artifact.meta["tier"] == "kling"


async def test_unsupported_models_raise_when_mock_fallback_disabled():
    adapter, calls = _make_adapter(allow_mock_fallback=False)

    with pytest.raises(RuntimeError, match="mock fallback disabled"):
        await adapter.generate_clip("item-abc", "seedance", 8, 1)

    assert calls == []


async def test_unknown_tier_raises_key_error():
    adapter, _ = _make_adapter()

    with pytest.raises(KeyError):
        await adapter.generate_clip("item-abc", "unknown", 8, 1)


async def test_generate_clip_raises_on_none_output():
    """Output nulo do SDK não pode virar Artifact com uri "None" — tem que ser erro."""
    adapter, _ = _make_adapter(output=None)

    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.generate_clip("item-abc", "ltx", 8, 1)


async def test_generate_clip_raises_on_empty_string_output():
    adapter, _ = _make_adapter(output="   ")

    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.generate_clip("item-abc", "ltx", 8, 1)


async def test_generate_clip_raises_on_empty_list_output():
    """Lista vazia do SDK vira erro — não pode indexar output[0] inexistente."""
    adapter, _ = _make_adapter(output=[])

    with pytest.raises(RuntimeError, match="output list is empty"):
        await adapter.generate_clip("item-abc", "ltx", 8, 1)


async def test_generate_clip_normalizes_dict_key_with_list_value():
    """Chave de vídeo cujo valor é lista → pega o primeiro elemento."""
    adapter, _ = _make_adapter(output={"video": ["https://cdn.replicate.com/keylist.mp4"]})

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/keylist.mp4"


async def test_generate_clip_raises_on_empty_dict_output():
    adapter, _ = _make_adapter(output={})

    with pytest.raises(RuntimeError, match="output dict is empty"):
        await adapter.generate_clip("item-abc", "ltx", 8, 1)


async def test_generate_clip_fallback_dict_first_value_empty_list_raises():
    """Dict sem chave de vídeo conhecida: fallback pega o primeiro valor; lista vazia é erro."""
    adapter, _ = _make_adapter(output={"other": []})

    with pytest.raises(RuntimeError, match="fallback list is empty"):
        await adapter.generate_clip("item-abc", "ltx", 8, 1)


async def test_generate_clip_fallback_dict_first_value_list():
    adapter, _ = _make_adapter(output={"other": ["https://cdn.replicate.com/fallbacklist.mp4"]})

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/fallbacklist.mp4"


async def test_generate_clip_fallback_dict_first_value_string():
    adapter, _ = _make_adapter(output={"other": "https://cdn.replicate.com/fallbackstr.mp4"})

    artifact = await adapter.generate_clip("item-abc", "ltx", 8, 1)

    assert artifact.uri == "https://cdn.replicate.com/fallbackstr.mp4"


async def test_submit_latentsync_prediction_with_version_string():
    created: list[dict[str, Any]] = []

    async def async_create(*, version=None, input=None, **params):
        created.append({"version": version, "input": input, "params": params})
        return SimpleNamespace(id="pred-ls-ver", status="starting", output=None, error=None)

    predictions = SimpleNamespace(async_create=async_create)
    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        prediction_client=SimpleNamespace(predictions=predictions),
        latentsync={"model": "bytedance/latentsync:ver_abc123", "resolution": "720p"},
    )

    pred = await adapter.submit_latentsync_prediction(
        video_uri="https://cdn.replicate.com/video.mp4",
        audio_uri="https://cdn.r2.com/audio.wav",
        resolution="720p",
    )

    assert pred.id == "pred-ls-ver"
    assert len(created) == 1
    assert created[0]["version"] == "ver_abc123"
    assert created[0]["input"] == {
        "video": "https://cdn.replicate.com/video.mp4",
        "audio": "https://cdn.r2.com/audio.wav",
    }
    assert "resolution" not in created[0]["input"]


async def test_submit_latentsync_prediction_default_bytedance_latentsync_uses_pinned_hash():
    created: list[dict[str, Any]] = []

    async def predictions_async_create(*, version=None, input=None, **params):
        created.append({"version": version, "input": input, "params": params})
        return SimpleNamespace(id="pred-ls-pinned", status="starting", output=None, error=None)

    client = SimpleNamespace(
        predictions=SimpleNamespace(async_create=predictions_async_create),
    )

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        prediction_client=client,
        latentsync={"model": "bytedance/latentsync"},
    )

    pred = await adapter.submit_latentsync_prediction(
        video_uri="https://cdn.replicate.com/video.mp4",
        audio_uri="https://cdn.r2.com/audio.wav",
    )

    assert pred.id == "pred-ls-pinned"
    assert len(created) == 1
    assert (
        created[0]["version"] == "637ce1919f807ca20da3a448ddc2743535d2853649574cd52a933120e9b9e293"
    )
    assert created[0]["input"] == {
        "video": "https://cdn.replicate.com/video.mp4",
        "audio": "https://cdn.r2.com/audio.wav",
    }


async def test_submit_latentsync_prediction_resolves_and_caches_unknown_model_version():
    created: list[dict[str, Any]] = []
    get_calls: list[str] = []

    async def models_async_get(model_name):
        get_calls.append(model_name)
        assert model_name == "custom/latentsync"
        return SimpleNamespace(latest_version=SimpleNamespace(id="latest_ver_999"))

    async def predictions_async_create(*, version=None, input=None, **params):
        created.append({"version": version, "input": input, "params": params})
        return SimpleNamespace(id="pred-ls-dynamic", status="starting", output=None, error=None)

    client = SimpleNamespace(
        models=SimpleNamespace(
            async_get=models_async_get,
        ),
        predictions=SimpleNamespace(async_create=predictions_async_create),
    )

    adapter = ReplicateVideoAdapter(
        tiers=TIERS,
        prediction_client=client,
        latentsync={"model": "custom/latentsync"},
    )

    pred1 = await adapter.submit_latentsync_prediction(
        video_uri="https://cdn.replicate.com/video1.mp4",
        audio_uri="https://cdn.r2.com/audio1.wav",
    )
    pred2 = await adapter.submit_latentsync_prediction(
        video_uri="https://cdn.replicate.com/video2.mp4",
        audio_uri="https://cdn.r2.com/audio2.wav",
    )

    assert pred1.id == "pred-ls-dynamic"
    assert pred2.id == "pred-ls-dynamic"
    assert len(get_calls) == 1  # Resolved once and cached
    assert len(created) == 2
    assert created[0]["version"] == "latest_ver_999"
    assert created[1]["version"] == "latest_ver_999"
