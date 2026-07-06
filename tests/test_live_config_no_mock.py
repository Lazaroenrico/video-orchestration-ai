"""Live config must not route production roles to mock adapters."""
from __future__ import annotations

from orchestrator.config import load_pipeline, load_providers


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
