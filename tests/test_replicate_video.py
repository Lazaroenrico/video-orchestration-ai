"""ReplicateVideoAdapter via SDK oficial, sem rede."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.graph.state import Artifact

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


def _make_adapter(output: Any = "https://cdn.replicate.com/clip.mp4", **kwargs: Any):
    calls: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict[str, Any]):
        calls.append({"ref": ref, "input": input})
        return output

    adapter = ReplicateVideoAdapter(tiers=TIERS, runner=fake_runner, **kwargs)
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


async def test_non_ltx_tiers_fallback_to_mock_clip():
    adapter, calls = _make_adapter()

    artifact = await adapter.generate_clip("item-abc", "kling", 8, 1)

    assert calls == []
    assert artifact.kind == "clip"
    assert artifact.meta["provider"] == "mock"
    assert artifact.meta["fallback_reason"] == "replicate_model_not_configured"
    assert artifact.meta["tier"] == "kling"


async def test_non_ltx_tiers_raise_when_mock_fallback_disabled():
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
