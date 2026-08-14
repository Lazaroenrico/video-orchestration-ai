"""Pure provider-neutral usage and cost contracts."""
from types import SimpleNamespace

import pytest


def test_normalize_model_gateway_alias():
    from orchestrator.tracing import _normalize_model

    assert _normalize_model("anthropic/claude-opus-4.8") == "claude-opus-4-8"
    assert _normalize_model("claude-opus-4-8") == "claude-opus-4-8"


def test_normalize_model_idempotent_and_other_models():
    from orchestrator.tracing import _normalize_model

    assert _normalize_model("claude-sonnet-5") == "claude-sonnet-5"
    assert _normalize_model("anthropic/claude-haiku-4.5") == "claude-haiku-4-5"


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
        "claude-opus-4-8", 1_000_000, 0,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert result is not None
    assert result["input_cost"] == pytest.approx(11.75)
    assert result["output_cost"] == pytest.approx(0.0)
    assert result["total_cost"] == pytest.approx(11.75)


def test_compute_llm_cost_unknown_model_returns_none():
    from orchestrator.tracing import compute_llm_cost

    assert compute_llm_cost("some-unknown-model", 100, 100) is None


def test_build_usage_metadata_tokens_and_cost():
    from orchestrator.tracing import build_usage_metadata

    usage = SimpleNamespace(input_tokens=100, output_tokens=50,
                            cache_read_input_tokens=20,
                            cache_creation_input_tokens=10)
    meta = build_usage_metadata(usage, "claude-opus-4-8")
    assert meta["input_tokens"] == 130
    assert meta["output_tokens"] == 50
    assert meta["total_tokens"] == 180
    assert meta["input_token_details"] == {"cache_read": 20, "cache_creation": 10}
    assert meta["total_cost"] == pytest.approx(meta["input_cost"] + meta["output_cost"])


def test_build_usage_metadata_handles_none_fields():
    from orchestrator.tracing import build_usage_metadata

    meta = build_usage_metadata(SimpleNamespace(input_tokens=None, output_tokens=None,
        cache_read_input_tokens=None, cache_creation_input_tokens=None), "claude-opus-4-8")
    assert meta["input_tokens"] == meta["output_tokens"] == meta["total_tokens"] == 0
    assert "input_token_details" not in meta


def test_build_usage_metadata_unknown_model_has_no_cost_keys():
    from orchestrator.tracing import build_usage_metadata

    meta = build_usage_metadata({"input_tokens": 100, "output_tokens": 50}, "unknown")
    assert not {"input_cost", "output_cost", "total_cost"} & meta.keys()


def test_build_usage_metadata_accepts_dict_usage():
    from orchestrator.tracing import build_usage_metadata

    meta = build_usage_metadata({"input_tokens": 10, "output_tokens": 5}, "claude-opus-4-8")
    assert meta["input_tokens"] == 10
    assert meta["output_tokens"] == 5
    assert meta["total_tokens"] == 15


def test_record_llm_usage_noop_offline(monkeypatch):
    from orchestrator import tracing

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    tracing.record_llm_usage(SimpleNamespace(input_tokens=1, output_tokens=1), "mock")


def test_record_llm_usage_attaches_when_tracing_on(monkeypatch):
    from orchestrator import tracing

    monkeypatch.setattr(tracing, "_HAS_LS", True)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    fake_run = SimpleNamespace(metadata={})
    monkeypatch.setattr(tracing, "get_current_run_tree", lambda: fake_run)
    tracing.record_llm_usage(SimpleNamespace(input_tokens=100, output_tokens=50), "claude-opus-4-8")
    assert fake_run.metadata["usage_metadata"]["input_tokens"] == 100
    assert fake_run.metadata["ls_model_name"] == "claude-opus-4-8"


def test_record_llm_usage_never_raises_on_bad_usage(monkeypatch):
    from orchestrator import tracing

    monkeypatch.setattr(tracing, "_HAS_LS", True)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(tracing, "get_current_run_tree", lambda: (_ for _ in ()).throw(RuntimeError("no run")))
    tracing.record_llm_usage(object(), "claude-opus-4-8")
