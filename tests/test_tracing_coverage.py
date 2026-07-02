"""Cobertura offline de tracing para nodes e adapters.

Esses testes não ligam LangSmith nem fazem rede. Eles validam os marcadores
offline anexados por ``@traced`` para garantir que a pipeline inteira ganhou
span nomeado sem depender de execução live.
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _trace_name(obj) -> str:
    return getattr(obj, "__trace_name__")


def test_all_pipeline_nodes_have_trace_markers():
    from orchestrator.graph import builder
    from orchestrator.nodes import stages

    expected = {
        stages.node_roster: "node.roster",
        stages.node_approval: "node.approval",
        stages.node_concepts: "node.concepts",
        stages.node_feedback: "node.feedback",
        stages.node_script: "node.script",
        stages.make_gen_node("ltx"): "node.video.ltx",
        stages.make_gen_node("kling"): "node.video.kling",
        stages.make_gen_node("seedance"): "node.video.seedance",
        stages.node_product_demo: "node.product_demo",
        stages.node_qc: "node.qc",
        stages.node_assembly: "node.assembly",
        stages.node_distribution: "node.distribution",
        stages.node_drop: "node.drop",
        builder.make_process_item_node(None): "node.process_item",
        builder.make_fan_out_node(): "node.fan_out",
        builder.make_script_route_node(["ltx"]): "node.script.route",
        builder.make_qc_route_node(["ltx"], max_attempts=1): "node.qc.route",
    }

    assert {_trace_name(fn) for fn in expected} == set(expected.values())


def test_product_demo_adapter_call_can_be_marked_as_step_5():
    from orchestrator.nodes import stages

    fn = stages.node_product_demo

    assert fn.__trace_metadata__["step"] == 5


def test_composite_adapter_public_methods_have_trace_markers():
    from orchestrator.registry import CompositeAdapter

    expected = {
        "generate_concepts": "adapter.llm.generate_concepts",
        "write_script": "adapter.llm.write_script",
        "build_creator": "adapter.creator.build_creator",
        "generate_clip": "adapter.video.generate_clip",
        "qc_check": "adapter.qc.qc_check",
        "assemble": "adapter.assembly.assemble",
        "distribute": "adapter.distribution.distribute",
    }

    for method, trace_name in expected.items():
        assert _trace_name(getattr(CompositeAdapter, method)) == trace_name

    assert CompositeAdapter.generate_clip.__trace_metadata__["step"] == "video"


def test_concrete_adapter_methods_have_trace_markers():
    from orchestrator.adapters.anthropic_llm import AnthropicLLMAdapter
    from orchestrator.adapters.creator_real import RealCreatorAdapter
    from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.adapters.openai_image import OpenAIImageAdapter
    from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter
    from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
    from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
    from orchestrator.adapters.topaz_upscale import TopazUpscaleAdapter

    expected = {
        MockAdapter.generate_concepts: "adapter.mock.generate_concepts",
        MockAdapter.write_script: "adapter.mock.write_script",
        MockAdapter.build_creator: "adapter.mock.build_creator",
        MockAdapter.generate_clip: "adapter.mock.generate_clip",
        MockAdapter.qc_check: "adapter.mock.qc_check",
        MockAdapter.assemble: "adapter.mock.assemble",
        MockAdapter.distribute: "adapter.mock.distribute",
        AnthropicLLMAdapter.generate_concepts: "adapter.anthropic.generate_concepts",
        AnthropicLLMAdapter.write_script: "adapter.anthropic.write_script",
        RealCreatorAdapter.build_creator: "adapter.creator_real.build_creator",
        OpenAIImageAdapter.generate_face: "adapter.openai_image.generate_face",
        TopazUpscaleAdapter.upscale: "adapter.topaz.upscale",
        ElevenLabsVoiceAdapter.create_voice: "adapter.elevenlabs.create_voice",
        ReplicateVideoAdapter.generate_clip: "adapter.replicate_video.generate_clip",
        ReplicateUpscaleAdapter.upscale: "adapter.replicate_upscale.upscale",
        ReplicateVoiceAdapter.create_voice: "adapter.replicate_voice.create_voice",
    }

    assert {_trace_name(fn) for fn in expected} == set(expected.values())
    assert MockAdapter.generate_clip.__trace_metadata__["step"] == "video"
    assert ReplicateVideoAdapter.generate_clip.__trace_metadata__["step"] == "video"


def test_anthropic_client_is_used_directly_without_wrapping(monkeypatch):
    """AnthropicLLMAdapter usa o client injetado diretamente.

    Fonte única de tokens/custo: ``record_llm_usage`` (chamado manualmente
    pelo adapter a partir de ``response.usage``) — não mais
    ``wrap_anthropic_client``, que criava uma run-filha duplicada e não
    reconhecia modelos novos/aliases de gateway no price-map do LangSmith.
    Ver docs/PROGRESS.md.
    """
    import orchestrator.adapters.anthropic_llm as anthropic_mod

    client = MagicMock()
    adapter = anthropic_mod.AnthropicLLMAdapter(client=client)

    assert adapter._client is client
