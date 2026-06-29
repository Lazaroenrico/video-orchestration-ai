"""CompositeAdapter: roteamento por papel + default mock (integração dos adapters reais)."""
from orchestrator.adapters.anthropic_llm import AnthropicLLMAdapter
from orchestrator.adapters.mock import MockAdapter
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
    assert isinstance(comp._by_role["distribution"], MockAdapter)
