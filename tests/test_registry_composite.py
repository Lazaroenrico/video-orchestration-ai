"""CompositeAdapter: roteamento por papel + default mock (integração dos adapters reais)."""
from orchestrator.adapters.anthropic_llm import AnthropicLLMAdapter
from orchestrator.adapters.integrity_qc import IntegrityQCAdapter
from orchestrator.adapters.mock import MockAdapter
from orchestrator.adapters.vercel_seedance_assembly import VercelSeedanceAssemblyAdapter
from orchestrator.registry import (
    ROLES,
    CompositeAdapter,
    build_adapter_from_providers,
    register_adapter,
)

PROVIDERS_EMPTY: dict = {}


def test_default_all_roles_share_one_mock(pipeline_cfg):
    comp = build_adapter_from_providers(PROVIDERS_EMPTY, pipeline_cfg)
    assert isinstance(comp, CompositeAdapter)
    # papéis ausentes caem em mock; e é a MESMA instância (preserva determinismo)
    instances = {id(comp._by_role[r]) for r in ROLES}
    assert len(instances) == 1
    assert isinstance(comp._by_role["llm"], MockAdapter)


async def test_routes_each_role_to_its_adapter(pipeline_cfg):
    class FakeLLM:
        async def generate_concepts(self, **kwargs):
            return [{"marker": "fake-llm"}]

    register_adapter("fake_llm", lambda pipeline: FakeLLM())
    comp = build_adapter_from_providers({"adapters": {"llm": "fake_llm"}}, pipeline_cfg)

    # o papel llm vai para o fake; os demais seguem mock
    out = await comp.generate_concepts(offer="o", n=1, seed="s")
    assert out == [{"marker": "fake-llm"}]
    assert isinstance(comp._by_role["video"], MockAdapter)
    assert isinstance(comp._by_role["creator"], MockAdapter)


def test_vercel_gateway_llm_routes_only_llm_role(monkeypatch, pipeline_cfg):
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test-gateway-key")
    comp = build_adapter_from_providers(
        {"adapters": {"llm": "vercel_gateway_llm"}},
        pipeline_cfg,
    )

    assert isinstance(comp._by_role["llm"], AnthropicLLMAdapter)
    assert isinstance(comp._by_role["creator"], MockAdapter)
    assert isinstance(comp._by_role["video"], MockAdapter)
    assert isinstance(comp._by_role["qc"], MockAdapter)
    assert isinstance(comp._by_role["assembly"], MockAdapter)


def test_live_qc_and_seedance_assembly_adapters_are_registered(pipeline_cfg):
    comp = build_adapter_from_providers(
        {
            "adapters": {
                "qc": "integrity_qc",
                "assembly": "vercel_seedance_assembly",
            }
        },
        pipeline_cfg,
    )

    assert isinstance(comp._by_role["qc"], IntegrityQCAdapter)
    assert isinstance(comp._by_role["assembly"], VercelSeedanceAssemblyAdapter)
    assert isinstance(comp._by_role["llm"], MockAdapter)


def test_composite_delegates_reroll_creator_voice_when_creator_role_has_it(pipeline_cfg):
    class CreatorWithReroll:
        async def reroll_creator_voice(self, **kwargs):
            return {"voice_id": "new"}

    register_adapter("creator_with_reroll", lambda pipeline: CreatorWithReroll())
    comp = build_adapter_from_providers(
        {"adapters": {"creator": "creator_with_reroll"}}, pipeline_cfg
    )

    reroll = getattr(comp, "reroll_creator_voice", None)
    assert callable(reroll)


def test_composite_hides_reroll_when_creator_role_lacks_it(pipeline_cfg):
    """MockAdapter não tem reroll → o stage precisa cair no fallback (getattr None)."""
    comp = build_adapter_from_providers(PROVIDERS_EMPTY, pipeline_cfg)
    assert getattr(comp, "reroll_creator_voice", None) is None


async def test_composite_routes_upscale_to_upscale_role(pipeline_cfg):
    class FakeUpscale:
        async def upscale(self, media_uri):
            return f"{media_uri}#4k"

    register_adapter("fake_upscale", lambda pipeline: FakeUpscale())
    comp = build_adapter_from_providers({"adapters": {"upscale": "fake_upscale"}}, pipeline_cfg)

    out = await comp.upscale("data:video/mp4;base64,AAA")
    assert out == "data:video/mp4;base64,AAA#4k"
    assert isinstance(comp._by_role["assembly"], MockAdapter)  # demais papéis seguem mock


async def test_live_upscale_role_is_passthrough(pipeline_cfg):
    from orchestrator.adapters.passthrough_upscale import PassthroughUpscaleAdapter

    comp = build_adapter_from_providers(
        {"adapters": {"upscale": "passthrough_upscale"}}, pipeline_cfg
    )
    assert isinstance(comp._by_role["upscale"], PassthroughUpscaleAdapter)
    assert await comp.upscale("/videos/run/assembled.mp4") == "/videos/run/assembled.mp4"


def test_composite_exposes_voice_subadapter_of_creator_role(pipeline_cfg):
    class VoiceSub:
        pass

    class CreatorWithVoice:
        def __init__(self):
            self.voice = VoiceSub()

    register_adapter("creator_with_voice", lambda pipeline: CreatorWithVoice())
    comp = build_adapter_from_providers(
        {"adapters": {"creator": "creator_with_voice"}}, pipeline_cfg
    )

    assert isinstance(comp.voice, VoiceSub)
    # Mock não tem sub-adapter de voz → atributo ausente, como antes.
    mock_comp = build_adapter_from_providers(PROVIDERS_EMPTY, pipeline_cfg)
    assert getattr(mock_comp, "voice", None) is None
