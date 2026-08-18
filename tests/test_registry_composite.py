"""Domain/media adapter registry boundaries."""

import pytest

from orchestrator.adapters.ffmpeg_assembly import FfmpegAssemblyAdapter
from orchestrator.adapters.integrity_qc import IntegrityQCAdapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.passthrough_upscale import PassthroughUpscaleAdapter
from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
from orchestrator.registry import (
    ROLES,
    CompositeAdapter,
    build_adapter_from_providers,
    register_adapter,
)


def test_default_roles_share_one_domain_mock(pipeline_cfg):
    composite = build_adapter_from_providers({}, pipeline_cfg)
    assert isinstance(composite, CompositeAdapter)
    assert set(composite._by_role) == set(ROLES)
    assert len({id(composite._by_role[role]) for role in ROLES}) == 1
    assert isinstance(composite._by_role["video"], MockAdapter)
    assert not hasattr(composite, "generate_concepts")


def test_language_provider_names_are_not_domain_adapters(pipeline_cfg):
    composite = build_adapter_from_providers(
        {"adapters": {"llm": "vercel_gateway_llm"}}, pipeline_cfg
    )
    assert "llm" not in composite._by_role
    assert not hasattr(composite, "generate_concepts")


def test_registered_domain_adapters_are_resolved_by_role(pipeline_cfg):
    composite = build_adapter_from_providers(
        {
            "adapters": {
                "video": "replicate",
                "qc": "integrity_qc",
                "assembly": "ffmpeg_assembly",
                "upscale": "passthrough_upscale",
            }
        },
        pipeline_cfg,
    )
    assert isinstance(composite._by_role["video"], ReplicateVideoAdapter)
    assert isinstance(composite._by_role["qc"], IntegrityQCAdapter)
    assert isinstance(composite._by_role["assembly"], FfmpegAssemblyAdapter)
    assert isinstance(composite._by_role["upscale"], PassthroughUpscaleAdapter)


def test_ffmpeg_assembly_adapter_is_registered(pipeline_cfg):
    composite = build_adapter_from_providers({"adapters": {"assembly": "ffmpeg_assembly"}}, pipeline_cfg)
    assert isinstance(composite._by_role["assembly"], FfmpegAssemblyAdapter)


def test_optional_creator_voice_capabilities_remain_domain_only(pipeline_cfg):
    class CreatorWithVoice:
        def __init__(self):
            self.voice = object()

    register_adapter("creator_with_voice_native", lambda _pipeline: CreatorWithVoice())
    composite = build_adapter_from_providers(
        {"adapters": {"creator": "creator_with_voice_native"}}, pipeline_cfg
    )
    assert composite.voice is not None
    assert getattr(build_adapter_from_providers({}, pipeline_cfg), "voice", None) is None


@pytest.mark.asyncio
async def test_composite_delegates_reroll_creator_voice_when_creator_role_has_it(pipeline_cfg):
    class CreatorWithReroll:
        async def reroll_creator_voice(self, **kwargs):
            return {"voice_id": "new", **kwargs}

    register_adapter("creator_with_reroll_native", lambda _pipeline: CreatorWithReroll())
    composite = build_adapter_from_providers(
        {"adapters": {"creator": "creator_with_reroll_native"}}, pipeline_cfg
    )
    assert await composite.reroll_creator_voice(marker="x") == {"voice_id": "new", "marker": "x"}


def test_composite_hides_optional_voice_reroll_when_creator_lacks_it(pipeline_cfg):
    composite = build_adapter_from_providers({}, pipeline_cfg)
    assert getattr(composite, "reroll_creator_voice", None) is None


async def test_composite_routes_upscale_to_upscale_role(pipeline_cfg):
    class FakeUpscale:
        async def upscale(self, media_uri):
            return f"{media_uri}#4k"

    register_adapter("fake_upscale_native", lambda _pipeline: FakeUpscale())
    composite = build_adapter_from_providers(
        {"adapters": {"upscale": "fake_upscale_native"}}, pipeline_cfg
    )
    assert await composite.upscale("data:video/mp4;base64,AAA") == "data:video/mp4;base64,AAA#4k"
    assert isinstance(composite._by_role["assembly"], MockAdapter)


async def test_live_upscale_role_is_passthrough(pipeline_cfg):
    composite = build_adapter_from_providers(
        {"adapters": {"upscale": "passthrough_upscale"}}, pipeline_cfg
    )
    assert isinstance(composite._by_role["upscale"], PassthroughUpscaleAdapter)
    assert await composite.upscale("/videos/run/assembled.mp4") == "/videos/run/assembled.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["design_voice_candidates", "finalize_voice", "reconcile_voice"]
)
async def test_composite_falls_back_to_creator_voice_subadapter(pipeline_cfg, method):
    sentinel = object()

    class VoiceSub:
        async def design_voice_candidates(self, *args, **kwargs):
            return sentinel

        async def finalize_voice(self, *args, **kwargs):
            return sentinel

        async def reconcile_voice(self, *args, **kwargs):
            return sentinel

    class CreatorWithVoice:
        def __init__(self):
            self.voice = VoiceSub()

    register_adapter("voice_subadapter_native", lambda _pipeline: CreatorWithVoice())
    composite = build_adapter_from_providers(
        {"adapters": {"creator": "voice_subadapter_native"}}, pipeline_cfg
    )
    assert await getattr(composite, method)("argument", field="value") is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["design_voice_candidates", "finalize_voice", "reconcile_voice"]
)
async def test_composite_reports_missing_voice_design_capability(pipeline_cfg, method):
    class CreatorWithoutVoice:
        pass

    register_adapter("creator_without_voice_native", lambda _pipeline: CreatorWithoutVoice())
    composite = build_adapter_from_providers(
        {"adapters": {"creator": "creator_without_voice_native"}}, pipeline_cfg
    )
    with pytest.raises(AttributeError, match=method):
        await getattr(composite, method)()


async def test_composite_prefers_creator_level_reconcile(pipeline_cfg):
    class CreatorWithReconcile:
        async def reconcile_voice(self, *args, **kwargs):
            return {"voice_ref": "creator-level"}

    register_adapter("creator_level_reconcile_native", lambda _pipeline: CreatorWithReconcile())
    composite = build_adapter_from_providers(
        {"adapters": {"creator": "creator_level_reconcile_native"}}, pipeline_cfg
    )
    assert await composite.reconcile_voice() == {"voice_ref": "creator-level"}


def test_judge_port_and_role_not_in_production_adapters():
    import orchestrator.adapters.base as base_mod
    assert not hasattr(base_mod, "JudgePort")
    assert "judge" not in ROLES


def test_providers_yaml_does_not_contain_judge_adapter():
    from orchestrator.config import load_providers
    for config_name in ("config", "config-mock", "config-staging"):
        providers = load_providers(config_name)
        adapters = providers.get("adapters", {})
        assert "judge" not in adapters, f"judge found in {config_name}/providers.yaml"
