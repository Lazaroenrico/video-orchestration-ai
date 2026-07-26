"""Persona doc (D35) — o personagem no papel antes de qualquer pixel.

A ordem real da pipeline é: **persona -> conceitos -> scripts -> imagem do creator**.
A persona é batch-level (uma por run/marca), não por script: o roster tem N creators
reutilizáveis para um batch de M itens (``roster[i % len(roster)]``), então uma persona
por script não teria como ser expressa por um creator compartilhado.

Estes testes travam o contrato da tool e o determinismo offline.
"""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.adapters.mock import MockAdapter

TIERS = [{"name": "ltx", "model": "m", "cost_per_second": 0.01, "max_concurrency": 4}]


class _SpyAdapter:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def write_persona(self, **kwargs: Any) -> Any:
        self.calls.append(("write_persona", kwargs))
        return self.result


class _PipelineSpyAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def write_persona(self, **kwargs: Any) -> str:
        self.calls.append(("write_persona", kwargs))
        return "PERSONA: Ana fala como especialista direta."

    async def generate_concepts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("generate_concepts", kwargs))
        return [{"id": "concept-1", "hook": "h", "offer": kwargs["offer"]}]

    async def write_script(self, **kwargs: Any) -> str:
        self.calls.append(("write_script", kwargs))
        return "HOOK: h\nBODY: usa a persona.\nCTA: testar."

    async def build_creator(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("build_creator", kwargs))
        return {
            "id": f"creator-{kwargs['index']}",
            "upscaled_base": "mock://image",
            "voice_id": "voice",
        }


def _config(adapter: Any) -> dict[str, Any]:
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": {},
            "run": {},
            "thread_id": "run-1",
        }
    }


async def test_write_persona_tool_delegates_and_validates():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.persona import write_persona_tool

    adapter = _SpyAdapter("PERSONA: Ana, 28.")
    ctx = tool_context_from_config(_config(adapter))

    out = await write_persona_tool(ctx, offer="serum X", brief="tom leve", revision=None)

    assert out == "PERSONA: Ana, 28."
    assert adapter.calls == [
        ("write_persona", {"offer": "serum X", "brief": "tom leve", "revision": None})
    ]


async def test_write_persona_tool_rejects_empty_persona():
    """Persona vazia não pode seguir: os scripts e a imagem do creator dependem dela."""
    from orchestrator.tools.base import ToolOutputError, tool_context_from_config
    from orchestrator.tools.persona import write_persona_tool

    ctx = tool_context_from_config(_config(_SpyAdapter("   ")))

    with pytest.raises(ToolOutputError, match="write_persona_tool"):
        await write_persona_tool(ctx, offer="o", brief=None)


def test_persona_is_registered_as_an_llm_stage():
    from orchestrator.tools.registry import get_tool_spec

    spec = get_tool_spec("write_persona")
    assert spec.stage == "persona"
    assert spec.role == "llm"
    assert spec.function_path == "orchestrator.tools.persona.write_persona_tool"


async def test_mock_write_persona_is_deterministic():
    """Determinismo offline: mesma entrada -> mesma persona (nada de random)."""
    adapter = MockAdapter(tiers=TIERS)

    a = await adapter.write_persona(offer="serum X", brief="tom leve")
    b = await adapter.write_persona(offer="serum X", brief="tom leve")
    c = await adapter.write_persona(offer="serum X", brief="tom seco")

    assert a == b
    assert a != c
    assert a.strip()


async def test_mock_write_persona_varies_with_revision():
    """A diretiva de refino do agent muda a persona (paridade com concepts/scripts)."""
    adapter = MockAdapter(tiers=TIERS)

    base = await adapter.write_persona(offer="serum X", brief=None)
    revised = await adapter.write_persona(offer="serum X", brief=None, revision="mais jovem")

    assert base != revised


async def test_composite_adapter_routes_write_persona_to_the_llm_role():
    """A persona é um port do papel llm — o composite delega como concepts/scripts."""
    from orchestrator.registry import CompositeAdapter

    llm = _SpyAdapter("PERSONA: X")
    composite = CompositeAdapter({"llm": llm})

    out = await composite.write_persona(offer="o", brief=None, revision=None)

    assert out == "PERSONA: X"
    assert llm.calls[0][0] == "write_persona"


def test_top_graph_orders_persona_before_concepts():
    from orchestrator.graph.builder import build_graph

    pipeline = {"tiers": TIERS, "batch": {"default_size": 1}, "roster": {"creators": 1}}
    app = build_graph(pipeline)
    nodes = set(app.get_graph().nodes)
    edges = {(edge.source, edge.target) for edge in app.get_graph().edges}

    assert "persona" in nodes
    assert ("persona", "concepts") in edges
    assert ("concepts", "scripts") in edges


async def test_persona_is_saved_and_propagated_to_downstream_stages():
    from orchestrator.nodes import stages

    adapter = _PipelineSpyAdapter()
    cfg = _config(adapter)
    cfg["configurable"]["pipeline"] = {
        "batch": {"default_size": 1},
        "roster": {"creators": 1},
        "clip": {"duration_seconds": 8},
    }
    cfg["configurable"]["run"] = {
        "platform": "tiktok",
        "creator_prompt": "CREATOR PROMPT: natural light.",
    }
    state: dict[str, Any] = {
        "run_id": "run-persona",
        "config": {"offer": "serum X", "batch_size": 1, "persona_brief": "tom leve"},
    }

    state.update(await stages.node_persona(state, cfg))
    state.update(await stages.node_concepts(state, cfg))
    state.update(await stages.node_scripts(state, cfg))
    roster_update = await stages.node_roster(state, cfg)

    assert state["persona"] == "PERSONA: Ana fala como especialista direta."
    assert adapter.calls[0] == (
        "write_persona",
        {"offer": "serum X", "brief": "tom leve", "revision": None},
    )
    concepts_call = adapter.calls[1][1]
    scripts_call = adapter.calls[2][1]
    creator_call = adapter.calls[3][1]
    assert concepts_call["persona"] == state["persona"]
    assert scripts_call["persona"] == state["persona"]
    assert creator_call["system_prompt"].startswith(state["persona"])
    assert "CREATOR PROMPT: natural light." in creator_call["system_prompt"]
    assert roster_update["roster"][0]["id"] == "creator-0"


def test_persona_stage_is_allowed_and_configured_for_live_and_mock():
    from orchestrator.agent_catalog import is_agent_stage_allowed
    from orchestrator.config import load_agent_catalog

    assert is_agent_stage_allowed("persona") is True

    live = load_agent_catalog("config").stage("persona")
    mock = load_agent_catalog("config-mock").stage("persona")

    assert live.tools == ("write_persona",)
    assert live.executor == "agent"
    assert live.agent_enabled is True
    assert mock.tools == ("write_persona",)
    assert mock.executor == "tool"
    assert mock.agent_enabled is False


def test_backend_node_labels_include_persona():
    from orchestrator.web.server import NODE_LABELS, PIPELINE_NODES

    assert "persona" in PIPELINE_NODES
    assert NODE_LABELS["persona"] == "Persona"
