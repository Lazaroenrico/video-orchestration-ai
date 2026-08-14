"""Live config must not route production roles to mock adapters."""
from __future__ import annotations

from orchestrator.adapters.elevenlabs_voice_design import ElevenLabsVoiceDesignAdapter
from orchestrator.adapters.ffmpeg_assembly import FfmpegAssemblyAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.config import load_agent_catalog, load_pipeline, load_providers
from orchestrator.registry import build_adapter_from_providers


def test_live_config_routes_all_runtime_roles_to_non_mock_adapters():
    providers = load_providers("config")
    adapters = providers["adapters"]

    runtime_roles = ("llm", "creator", "video", "qc", "assembly")
    assert {role: adapters.get(role) for role in runtime_roles} == {
        "llm": "vercel_gateway_llm",
        "creator": "creator_vercel_elevenlabs_design",
        "video": "replicate",
        "qc": "integrity_qc",
        "assembly": "ffmpeg_assembly",
    }
    assert all(adapters[role] != "mock" for role in runtime_roles)
    assert [
        role
        for role, adapter_name in adapters.items()
        if "replicate" in adapter_name
    ] == ["video"]


def test_live_config_uses_pruna_p_video_for_all_clips():
    pipeline = load_pipeline("config")

    assert pipeline["video"]["allow_mock_fallback"] is False
    assert pipeline["video"]["product_demo_tier"] == "pruna"
    assert [
        (tier["name"], tier["model"], tier["cost_per_second"])
        for tier in pipeline["tiers"]
    ] == [
        ("pruna", "prunaai/p-video", 0.01),
    ]
    assert pipeline["clip"]["fps"] == 24
    assert pipeline["clip"]["draft"] is True
    assert pipeline["assembly"] == {
        "final_duration_seconds": 16,
        "narration_target_seconds": 14,
        "narration_max_words": 35,
        "audio_speedup_max": 1.10,
        "resolution": "1080x1920",
        "fps": 24,
        "timeout_seconds": 300,
        "allow_mock_fallback": False,
    }


def test_live_runtime_uses_replicate_for_clips_and_ffmpeg_for_assembly(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-replicate-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs-key")
    composite = build_adapter_from_providers(
        load_providers("config"),
        load_pipeline("config"),
    )

    assert isinstance(
        composite._by_role["creator"].voice,
        ElevenLabsVoiceDesignAdapter,
    )
    assert isinstance(composite._by_role["video"], ReplicateVideoAdapter)
    assert isinstance(composite._by_role["assembly"], FfmpegAssemblyAdapter)


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

    for stage in (
        "video",
        "roster",
        "qc",
        "voiceover",
        "assembly",
        "upscale",
    ):
        spec = catalog.stage(stage)
        assert spec.executor == "tool", f"{stage} deve permanecer em modo tool"
        assert spec.agent_enabled is False


def test_live_config_allows_one_bounded_script_correction():
    pipeline = load_pipeline("config")
    agent = pipeline["agent"]

    assert agent["max_steps"] == 2
    assert agent["max_tool_calls"] == 1
    assert agent["max_steps_by_stage"] == {"scripts": 3}
    assert agent["max_tool_calls_by_stage"] == {"scripts": 2}
