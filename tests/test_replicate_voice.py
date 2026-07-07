"""Testes do ReplicateVoiceAdapter — offline, com runner injetável (SDK replicate)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from orchestrator.adapters.base import VoiceProfile
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter

FAKE_ELEVENLABS_MODEL = "acme/elevenlabs-tts:abc123"


def _make_adapter(output: Any = "https://cdn.replicate.com/voice.wav", **kwargs):
    captured: list[dict[str, Any]] = []

    async def fake_runner(ref: str, input: dict | None = None, **_: Any) -> Any:
        captured.append({"ref": ref, "input": input})
        return output

    kwargs.setdefault("model", FAKE_ELEVENLABS_MODEL)
    adapter = ReplicateVoiceAdapter(runner=fake_runner, **kwargs)
    return adapter, captured


def test_requires_replicate_elevenlabs_model_env(monkeypatch):
    monkeypatch.delenv("REPLICATE_ELEVENLABS_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="REPLICATE_ELEVENLABS_MODEL"):
        ReplicateVoiceAdapter()


def test_uses_replicate_elevenlabs_model_from_env(monkeypatch):
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", FAKE_ELEVENLABS_MODEL)

    adapter = ReplicateVoiceAdapter(runner=lambda *args, **kwargs: None)

    assert adapter.model == FAKE_ELEVENLABS_MODEL


async def test_create_voice_returns_string():
    adapter, _ = _make_adapter(output="https://cdn.replicate.com/v.wav")
    result = await adapter.create_voice(0)
    assert isinstance(result, str)
    assert result == "https://cdn.replicate.com/v.wav"


async def test_create_voice_parses_dict_audio_out():
    """Modelos de áudio podem devolver {'audio_out': url}; o adapter extrai a URL."""
    adapter, _ = _make_adapter(output={"audio_out": "https://cdn.replicate.com/voice.wav"})
    result = await adapter.create_voice(1)
    assert result == "https://cdn.replicate.com/voice.wav"


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
    assert captured[0]["ref"] == FAKE_ELEVENLABS_MODEL


async def test_create_voice_sends_prompt_with_index():
    adapter, captured = _make_adapter()
    await adapter.create_voice(3)
    assert "3" in captured[0]["input"]["text"]


async def test_create_voice_keeps_legacy_prompt_without_profile():
    adapter, captured = _make_adapter()
    await adapter.create_voice(4)
    assert captured[0]["input"]["text"] == "creator voice 4"


async def test_create_voice_includes_profile_preset_and_prompt():
    adapter, captured = _make_adapter()
    profile = VoiceProfile(preset="female", prompt="Warm, friendly beauty creator voice.")
    await adapter.create_voice(4, voice_profile=profile)
    text = captured[0]["input"]["text"]
    assert "female" in text
    assert "Warm, friendly beauty creator voice." in text
    assert "creator voice 4" in text


async def test_create_voice_uses_configurable_input_fields(monkeypatch):
    monkeypatch.setenv("REPLICATE_ELEVENLABS_TEXT_FIELD", "script")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_VOICE_FIELD", "voice")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL_ID_FIELD", "model_id")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_VOICE_ID_FEMALE", "voice-female")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    monkeypatch.setenv(
        "REPLICATE_ELEVENLABS_INPUT_JSON",
        '{"output_format":"mp3_44100_128"}',
    )
    adapter, captured = _make_adapter()

    await adapter.create_voice(
        4,
        voice_profile=VoiceProfile(preset="female", prompt="Warm UGC narration."),
    )

    body = captured[0]["input"]
    assert body["script"].startswith("creator voice 4")
    assert body["voice"] == "voice-female"
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["output_format"] == "mp3_44100_128"


async def test_turbo_v25_sends_script_under_prompt(monkeypatch):
    """Regressão do 422 `input: prompt is required`: com TEXT_FIELD=prompt o script vai
    sob `prompt` e nunca sob `text` (o campo que causava a falha no turbo-v2.5)."""
    monkeypatch.setenv("REPLICATE_ELEVENLABS_TEXT_FIELD", "prompt")
    adapter, captured = _make_adapter()
    await adapter.create_voice(3)
    body = captured[0]["input"]
    assert "prompt" in body
    assert "3" in body["prompt"]
    assert "text" not in body


async def test_voice_pool_no_repeat_across_creators(monkeypatch):
    """Pool de vozes por gênero: creators do mesmo preset recebem vozes distintas."""
    monkeypatch.setenv("REPLICATE_ELEVENLABS_VOICE_FIELD", "voice")
    monkeypatch.setenv("REPLICATE_ELEVENLABS_VOICE_ID_FEMALE", "v1,v2,v3")
    adapter, captured = _make_adapter()
    profile = VoiceProfile(preset="female", prompt="Warm UGC narration.")
    for i in range(3):
        await adapter.create_voice(i, voice_profile=profile)
    voices = [c["input"]["voice"] for c in captured]
    assert voices == ["v1", "v2", "v3"]
    assert len(set(voices)) == 3


async def test_different_indices_produce_different_prompts():
    adapter, captured = _make_adapter()
    await adapter.create_voice(0)
    await adapter.create_voice(5)
    assert captured[0]["input"]["text"] != captured[1]["input"]["text"]


async def test_create_voice_propagates_runner_error():
    calls = 0

    async def failing_runner(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise RuntimeError("voice boom")

    adapter = ReplicateVoiceAdapter(model=FAKE_ELEVENLABS_MODEL, runner=failing_runner)
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

    adapter = ReplicateVoiceAdapter(
        model=FAKE_ELEVENLABS_MODEL, runner=flaky_runner, backoff_base=0
    )
    result = await adapter.create_voice(0)
    assert result == "https://cdn.replicate.com/voice.wav"
    assert calls == 3


async def test_create_voice_raises_after_exhausting_retries():
    calls = 0

    async def always_timeout(ref: str, input: dict | None = None, **_: Any):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("connect failed")

    adapter = ReplicateVoiceAdapter(
        model=FAKE_ELEVENLABS_MODEL,
        runner=always_timeout,
        max_retries=2,
        backoff_base=0,
    )
    with pytest.raises(httpx.ConnectTimeout):
        await adapter.create_voice(0)
    assert calls == 3


async def test_create_voice_raises_on_none_output():
    """Output nulo do SDK não pode virar voice_id "None" — tem que ser erro."""
    adapter, _ = _make_adapter(output=None)
    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.create_voice(0)


async def test_create_voice_raises_on_empty_string_output():
    adapter, _ = _make_adapter(output="   ")
    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.create_voice(0)


async def test_create_voice_raises_on_empty_dict_output():
    """Dict vazio não pode virar StopIteration nem voice_id lixo."""
    adapter, _ = _make_adapter(output={})
    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.create_voice(0)


async def test_create_voice_raises_on_dict_known_key_null_value():
    """Chave de áudio conhecida com valor nulo não pode virar voice_id "None"."""
    adapter, _ = _make_adapter(output={"audio_out": None})
    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.create_voice(0)


async def test_create_voice_raises_on_dict_fallback_null_value():
    """Fallback (sem chave conhecida) com primeiro valor nulo também é erro."""
    adapter, _ = _make_adapter(output={"something": None})
    with pytest.raises(RuntimeError, match="output.*empty"):
        await adapter.create_voice(0)
