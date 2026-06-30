"""Testes do ReplicateUpscaleAdapter — offline, com runner injetável (SDK replicate)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter


def _make_adapter(output: Any = "https://cdn.replicate.com/upscaled.png", **kwargs):
    """Cria um adapter com runner falso que captura ref/input e devolve `output`."""
    captured: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict | None = None, **_: Any) -> Any:
        captured.append({"ref": ref, "input": input})
        return output

    adapter = ReplicateUpscaleAdapter(runner=fake_runner, **kwargs)
    return adapter, captured


async def test_upscale_returns_string_url():
    adapter, _ = _make_adapter(output="https://cdn.replicate.com/up.png")
    result = await adapter.upscale("https://example.com/image.png")
    assert result == "https://cdn.replicate.com/up.png"
    assert isinstance(result, str)


async def test_upscale_coerces_file_output_to_str():
    """O SDK devolve um FileOutput (URL-like); o adapter deve coagir para str."""

    class FakeFileOutput:
        def __init__(self, url: str) -> None:
            self._url = url

        def __str__(self) -> str:
            return self._url

    adapter, _ = _make_adapter(output=FakeFileOutput("https://cdn.replicate.com/f.png"))
    result = await adapter.upscale("https://example.com/img.png")
    assert result == "https://cdn.replicate.com/f.png"


async def test_upscale_uses_correct_model_ref():
    adapter, captured = _make_adapter()
    await adapter.upscale("https://example.com/img.png")
    # Default ref pina o version hash (community model exige) → owner/name:version
    assert captured[0]["ref"].startswith("nightmareai/real-esrgan:")


async def test_upscale_sends_image_and_scale_input():
    adapter, captured = _make_adapter()
    await adapter.upscale("https://example.com/face.png")
    inp = captured[0]["input"]
    assert inp["image"] == "https://example.com/face.png"
    assert inp["scale"] == 4


async def test_upscale_respects_custom_model_and_scale():
    adapter, captured = _make_adapter(model="other/upscaler", scale=2)
    await adapter.upscale("https://example.com/x.png")
    assert captured[0]["ref"] == "other/upscaler"
    assert captured[0]["input"]["scale"] == 2


async def test_upscale_propagates_runner_error():
    calls = 0

    async def failing_runner(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise RuntimeError("replicate boom")

    adapter = ReplicateUpscaleAdapter(runner=failing_runner)
    with pytest.raises(RuntimeError, match="replicate boom"):
        await adapter.upscale("https://example.com/img.png")
    # RuntimeError não é erro de transporte → propaga na 1ª, sem retry.
    assert calls == 1


async def test_upscale_retries_on_connect_timeout_then_succeeds():
    calls = 0

    async def flaky_runner(ref: str, input: dict | None = None, **_: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectTimeout("connect failed")
        return "https://cdn.replicate.com/ok.png"

    adapter = ReplicateUpscaleAdapter(runner=flaky_runner, backoff_base=0)
    result = await adapter.upscale("https://example.com/img.png")
    assert result == "https://cdn.replicate.com/ok.png"
    assert calls == 3  # 2 falhas + 1 sucesso


async def test_upscale_raises_after_exhausting_retries():
    calls = 0

    async def always_timeout(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("connect failed")

    adapter = ReplicateUpscaleAdapter(runner=always_timeout, max_retries=2, backoff_base=0)
    with pytest.raises(httpx.ConnectTimeout):
        await adapter.upscale("https://example.com/img.png")
    assert calls == 3  # tentativa inicial + 2 retries
