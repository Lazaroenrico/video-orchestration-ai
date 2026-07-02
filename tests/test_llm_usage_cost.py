"""tests/test_llm_usage_cost.py — captura de tokens + custo LLM no LangSmith.

Cobre offline (sem langsmith ativo) as funções puras de tracing.py:
- _normalize_model
- compute_llm_cost
- build_usage_metadata
- record_llm_usage

E a integração com o AnthropicLLMAdapter (generate_concepts chama
record_llm_usage com response.usage).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# _normalize_model                                                             #
# --------------------------------------------------------------------------- #

def test_normalize_model_gateway_alias():
    from orchestrator.tracing import _normalize_model

    assert _normalize_model("anthropic/claude-opus-4.8") == "claude-opus-4-8"
    assert _normalize_model("claude-opus-4-8") == "claude-opus-4-8"


def test_normalize_model_idempotent_and_other_models():
    from orchestrator.tracing import _normalize_model

    assert _normalize_model("claude-sonnet-5") == "claude-sonnet-5"
    assert _normalize_model("anthropic/claude-haiku-4.5") == "claude-haiku-4-5"


# --------------------------------------------------------------------------- #
# compute_llm_cost                                                             #
# --------------------------------------------------------------------------- #

def test_compute_llm_cost_opus():
    from orchestrator.tracing import compute_llm_cost

    result = compute_llm_cost("claude-opus-4-8", 1_000_000, 1_000_000)
    assert result is not None
    assert result["input_cost"] == pytest.approx(5.0)
    assert result["output_cost"] == pytest.approx(25.0)
    assert result["total_cost"] == pytest.approx(30.0)


def test_compute_llm_cost_with_cache():
    from orchestrator.tracing import compute_llm_cost

    result = compute_llm_cost(
        "claude-opus-4-8",
        input_tokens=1_000_000,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert result is not None
    # input_cost = 1M*5.0/1e6 + 1M*0.50/1e6 + 1M*6.25/1e6 = 5.0 + 0.5 + 6.25
    assert result["input_cost"] == pytest.approx(5.0 + 0.5 + 6.25)
    assert result["output_cost"] == pytest.approx(0.0)
    assert result["total_cost"] == pytest.approx(11.75)


def test_compute_llm_cost_unknown_model_returns_none():
    from orchestrator.tracing import compute_llm_cost

    assert compute_llm_cost("some-unknown-model", 100, 100) is None


# --------------------------------------------------------------------------- #
# build_usage_metadata                                                         #
# --------------------------------------------------------------------------- #

def test_build_usage_metadata_tokens_and_cost():
    from orchestrator.tracing import build_usage_metadata

    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=20,
        cache_creation_input_tokens=10,
    )
    meta = build_usage_metadata(usage, "claude-opus-4-8")

    assert meta["input_tokens"] == 130  # 100 + 20 + 10
    assert meta["output_tokens"] == 50
    assert meta["total_tokens"] == 180
    assert meta["input_token_details"] == {"cache_read": 20, "cache_creation": 10}
    assert meta["input_cost"] > 0
    assert meta["output_cost"] > 0
    assert meta["total_cost"] == pytest.approx(meta["input_cost"] + meta["output_cost"])


def test_build_usage_metadata_handles_none_fields():
    from orchestrator.tracing import build_usage_metadata

    usage = SimpleNamespace(
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )
    meta = build_usage_metadata(usage, "claude-opus-4-8")

    assert meta["input_tokens"] == 0
    assert meta["output_tokens"] == 0
    assert meta["total_tokens"] == 0
    assert "input_token_details" not in meta


def test_build_usage_metadata_unknown_model_has_no_cost_keys():
    from orchestrator.tracing import build_usage_metadata

    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    meta = build_usage_metadata(usage, "totally-unknown-model")

    assert "input_cost" not in meta
    assert "output_cost" not in meta
    assert "total_cost" not in meta


def test_build_usage_metadata_accepts_dict_usage():
    from orchestrator.tracing import build_usage_metadata

    usage = {"input_tokens": 10, "output_tokens": 5}
    meta = build_usage_metadata(usage, "claude-opus-4-8")

    assert meta["input_tokens"] == 10
    assert meta["output_tokens"] == 5
    assert meta["total_tokens"] == 15


# --------------------------------------------------------------------------- #
# record_llm_usage                                                             #
# --------------------------------------------------------------------------- #

def test_record_llm_usage_noop_offline(monkeypatch):
    from orchestrator import tracing as tracing_mod

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    usage = SimpleNamespace(
        input_tokens=1, output_tokens=1,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    # Não deve lançar mesmo sem run ativa / tracing desligado.
    tracing_mod.record_llm_usage(usage, "claude-opus-4-8")


def test_record_llm_usage_attaches_when_tracing_on(monkeypatch):
    from orchestrator import tracing as tracing_mod

    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    fake_run = SimpleNamespace(metadata={})
    monkeypatch.setattr(tracing_mod, "get_current_run_tree", lambda: fake_run)

    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    tracing_mod.record_llm_usage(usage, "claude-opus-4-8")

    assert "usage_metadata" in fake_run.metadata
    assert fake_run.metadata["usage_metadata"]["input_tokens"] == 100
    assert fake_run.metadata["ls_model_name"] == "claude-opus-4-8"


def test_record_llm_usage_never_raises_on_bad_usage(monkeypatch):
    from orchestrator import tracing as tracing_mod

    monkeypatch.setattr(tracing_mod, "_HAS_LS", True)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    def boom():
        raise RuntimeError("no run tree")

    monkeypatch.setattr(tracing_mod, "get_current_run_tree", boom)

    # Não deve lançar mesmo se get_current_run_tree explodir.
    tracing_mod.record_llm_usage(object(), "claude-opus-4-8")


# --------------------------------------------------------------------------- #
# Integração — AnthropicLLMAdapter chama record_llm_usage                     #
# --------------------------------------------------------------------------- #

class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 42
        self.output_tokens = 7
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.stop_reason = "end_turn"
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> _FakeResponse:
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


@pytest.mark.asyncio
async def test_adapter_generate_concepts_calls_record_llm_usage(monkeypatch):
    import json

    from orchestrator.adapters import anthropic_llm as anthropic_mod

    payload = json.dumps({
        "concepts": [
            {
                "id": "concept-0000",
                "offer": "serum X",
                "hook": "hook",
                "angle": "problem",
                "hook_style": "problem",
                "format": "talking_head",
            }
        ]
    })
    response = _FakeResponse(payload)
    client = _FakeClient(response)

    calls = []

    def fake_record(usage, model):
        calls.append((usage, model))

    monkeypatch.setattr(anthropic_mod, "record_llm_usage", fake_record)

    adapter = anthropic_mod.AnthropicLLMAdapter(client=client)
    await adapter.generate_concepts(offer="serum X", n=1, seed="s1")

    assert len(calls) == 1
    assert calls[0][0] is response.usage
    assert calls[0][1] == adapter.model
