"""Live config must not route production roles to mock adapters."""
from __future__ import annotations

from orchestrator.config import load_agent_catalog, load_pipeline, load_providers


def test_live_config_routes_all_runtime_roles_to_non_mock_adapters():
    providers = load_providers("config")
    adapters = providers["adapters"]

    runtime_roles = ("llm", "creator", "video", "qc", "assembly")
    assert {role: adapters.get(role) for role in runtime_roles} == {
        "llm": "vercel_gateway_llm",
        "creator": "creator_real_replicate",
        "video": "replicate",
        "qc": "integrity_qc",
        "assembly": "vercel_seedance_assembly",
    }
    assert all(adapters[role] != "mock" for role in runtime_roles)


def test_live_config_disables_replicate_mock_fallback():
    pipeline = load_pipeline("config")

    assert pipeline["video"]["allow_mock_fallback"] is False


def test_live_config_activates_agent_mode_only_on_creative_stages():
    """Media/QC/assembly remain deterministic adapters outside prompt authority."""
    catalog = load_agent_catalog("config")

    for stage in ("concepts", "scripts", "creator_profiles"):
        spec = catalog.stage(stage)
        assert spec.executor == "agent", f"{stage} deveria rodar em modo agent"
        assert spec.agent_enabled is True, f"{stage} precisa de agent_enabled: true"
        assert spec.system_prompt
        assert spec.prompt_hash
        assert spec.schema_version == "creative-v2"

    for stage in ("persona", "video", "roster", "qc", "assembly", "upscale"):
        spec = catalog.stage(stage)
        assert spec.executor == "tool", f"{stage} deve permanecer em modo tool"
        assert spec.agent_enabled is False


def test_live_config_caps_each_creative_agent_to_one_submission():
    pipeline = load_pipeline("config")
    agent = pipeline["agent"]

    assert agent["max_steps"] == 2
    assert agent["max_tool_calls"] == 1
    assert "max_steps_by_stage" not in agent
    assert "max_tool_calls_by_stage" not in agent
