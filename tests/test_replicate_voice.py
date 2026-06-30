"""Testes do ReplicateVoiceAdapter — offline, com runner injetável (SDK replicate)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter


def _make_adapter(output: Any = "https://cdn.replicate.com/voice.wav", **kwargs):
    captured: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict | None = None, **_: Any) -> Any:
        captured.append({"ref": ref, "input": input})
        return output

    adapter = ReplicateVoiceAdapter(runner=fake_runner, **kwargs)
    return adapter, captured


async def test_create_voice_returns_string():
    adapter, _ = _make_adapter(output="https://cdn.replicate.com/v.wav")
    result = await adapter.create_voice(0)
    assert isinstance(result, str)
    assert result == "https://cdn.replicate.com/v.wav"


async def test_create_voice_parses_dict_audio_out():
    """Bark devolve dict tipo {'audio_out': url}; o adapter deve extrair a URL."""
    adapter, _ = _make_adapter(output={"audio_out": "https://cdn.replicate.com/bark.wav"})
    result = await adapter.create_voice(1)
    assert result == "https://cdn.replicate.com/bark.wav"


async def test_create_voice_parses_dict_first_value_fallback():
    """Se não houver chave conhecida, usa o primeiro valor do dict."""
    adapter, _ = _make_adapter(output={"something": "https://cdn.replicate.com/x.wav"})
    result = await adapter.create_voice(0)
    assert result == "https://cdn.replicate.com/x.wav"


async def test_create_voice_coerces_file_output():
    class FakeFileOutput:
        def __str__(self) -> str:
            return "https://cdn.replicate.com/file.wav"

    adapter, _ = _make_adapter(output=FakeFileOutput())
    result = await adapter.create_voice(2)
    assert result == "https://cdn.replicate.com/file.wav"


async def test_create_voice_uses_correct_model_ref():
    adapter, captured = _make_adapter()
    await adapter.create_voice(0)
    # Default ref pina o version hash (community model exige) → owner/name:version
    assert captured[0]["ref"].startswith("suno-ai/bark:")


async def test_create_voice_sends_prompt_with_index():
    adapter, captured = _make_adapter()
    await adapter.create_voice(3)
    assert "3" in captured[0]["input"]["prompt"]


async def test_different_indices_produce_different_prompts():
    adapter, captured = _make_adapter()
    await adapter.create_voice(0)
    await adapter.create_voice(5)
    assert captured[0]["input"]["prompt"] != captured[1]["input"]["prompt"]


async def test_create_voice_propagates_runner_error():
    calls = 0

    async def failing_runner(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise RuntimeError("voice boom")

    adapter = ReplicateVoiceAdapter(runner=failing_runner)
    with pytest.raises(RuntimeError, match="voice boom"):
        await adapter.create_voice(0)
    assert calls == 1  # RuntimeError não retenta


async def test_create_voice_retries_on_connect_timeout_then_succeeds():
    calls = 0

    async def flaky_runner(ref: str, input: dict | None = None, **_: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectTimeout("connect failed")
        return "https://cdn.replicate.com/voice.wav"

    adapter = ReplicateVoiceAdapter(runner=flaky_runner, backoff_base=0)
    result = await adapter.create_voice(0)
    assert result == "https://cdn.replicate.com/voice.wav"
    assert calls == 3


async def test_create_voice_raises_after_exhausting_retries():
    calls = 0

    async def always_timeout(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("connect failed")

    adapter = ReplicateVoiceAdapter(runner=always_timeout, max_retries=2, backoff_base=0)
    with pytest.raises(httpx.ConnectTimeout):
        await adapter.create_voice(0)
    assert calls == 3
