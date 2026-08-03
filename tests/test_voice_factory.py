from __future__ import annotations

import pytest

from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
from orchestrator.adapters.elevenlabs_voice_design import ElevenLabsVoiceDesignAdapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
from orchestrator.adapters.voice_factory import build_voice_adapter
from orchestrator.config import load_pipeline


def test_voice_factory_rejects_unknown_explicit_configuration() -> None:
    with pytest.raises(ValueError, match="unsupported voice configuration"):
        build_voice_adapter(
            {
                "tiers": [],
                "voice": {"mode": "mystery", "provider": "elevenlabs"},
            }
        )


def test_voice_factory_rejects_non_mapping_voice_configuration() -> None:
    with pytest.raises(ValueError, match="voice configuration must be a mapping"):
        build_voice_adapter({"voice": "elevenlabs"})


@pytest.mark.parametrize("field", ["retry", "costs"])
def test_voice_factory_rejects_non_mapping_policy_blocks(field: str) -> None:
    with pytest.raises(ValueError, match=rf"voice\.{field} configuration"):
        build_voice_adapter(
            {
                "voice": {
                    "mode": "designed",
                    "provider": "elevenlabs",
                    field: "invalid",
                }
            }
        )


def test_voice_factory_keeps_legacy_fallback_only_when_voice_block_is_absent() -> None:
    assert isinstance(build_voice_adapter({}), ElevenLabsVoiceAdapter)


@pytest.mark.parametrize(
    ("voice", "expected_type"),
    [
        ({"mode": "replicate", "provider": "replicate"}, ReplicateVoiceAdapter),
        ({"mode": "legacy", "provider": "elevenlabs"}, ElevenLabsVoiceAdapter),
    ],
)
def test_voice_factory_builds_explicit_compatibility_adapters(
    voice: dict[str, str], expected_type: type, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPLICATE_ELEVENLABS_MODEL", "owner/model")
    assert isinstance(build_voice_adapter({"voice": voice}), expected_type)


def test_voice_factory_builds_mock_with_pipeline_tiers_and_latency() -> None:
    adapter = build_voice_adapter(
        {
            "tiers": [{"name": "standard", "cost_per_second": 0.0}],
            "latency": 0.25,
            "voice": {"mode": "mock", "provider": "mock"},
        }
    )

    assert isinstance(adapter, MockAdapter)
    assert list(adapter.tiers) == ["standard"]
    assert adapter.latency == 0.25


def test_voice_factory_forwards_all_direct_elevenlabs_policies() -> None:
    adapter = build_voice_adapter(
        {
            "voice": {
                "mode": "designed",
                "provider": "elevenlabs",
                "design_model": "design-v3",
                "tts_model": "tts-v2",
                "request_timeout_seconds": 77,
                "concurrency": 1,
                "candidates_per_creator": 2,
                "retry": {
                    "max_retries": 4,
                    "backoff_base_seconds": 0.5,
                    "max_delay_seconds": 7,
                },
                "costs": {
                    "design_per_candidate_usd": 0.02,
                    "tts_per_1000_chars_usd": 0.04,
                    "cost_source": "estimate",
                },
            }
        }
    )

    assert isinstance(adapter, ElevenLabsVoiceDesignAdapter)
    assert adapter.design_model == "design-v3"
    assert adapter.tts_model == "tts-v2"
    assert adapter.timeout_seconds == 77
    assert adapter.max_concurrency == 1
    assert adapter.max_candidates == 2
    assert adapter.max_retries == 4
    assert adapter.retry_backoff_seconds == 0.5
    assert adapter.retry_max_delay_seconds == 7
    assert adapter.design_cost_per_candidate_usd == 0.02
    assert adapter.tts_cost_per_1000_chars_usd == 0.04
    assert adapter.cost_source == "estimate"


@pytest.mark.parametrize("config_dir", ["config-mock", "config-staging"])
def test_offline_profiles_select_zero_cost_mock_voice(config_dir: str) -> None:
    pipeline = load_pipeline(config_dir)

    assert pipeline["voice"]["mode"] == "mock"
    assert pipeline["voice"]["provider"] == "mock"
    adapter = build_voice_adapter(pipeline)
    assert isinstance(adapter, MockAdapter)
