"""Offline tracing contracts for the native runtime and domain adapters."""


def _trace_name(obj) -> str:
    return getattr(obj, "__trace_name__")


def test_pipeline_nodes_have_trace_markers():
    from orchestrator.graph import builder
    from orchestrator.nodes import stages

    functions = (
        stages.node_approval,
        stages.node_roster,
        stages.node_concepts,
        stages.node_scripts,
        stages.node_concept_review,
        stages.node_feedback,
        stages.make_gen_node("ltx"),
        stages.node_product_demo,
        stages.node_qc,
        stages.node_assembly,
        stages.node_drop,
        builder.make_process_item_node(None),
        builder.make_fan_out_node(),
        builder.make_script_route_node(["ltx"]),
        builder.make_qc_route_node(["ltx"], max_attempts=1),
    )
    assert all(_trace_name(function).startswith("node.") for function in functions)


def test_product_demo_trace_keeps_step_metadata():
    from orchestrator.nodes import stages

    assert stages.node_product_demo.__trace_metadata__["step"] == 5


def test_domain_composite_methods_have_trace_markers():
    from orchestrator.registry import CompositeAdapter

    for method in ("build_creator", "generate_clip", "qc_check", "assemble", "upscale"):
        assert _trace_name(getattr(CompositeAdapter, method)).startswith("adapter.")
    assert not hasattr(CompositeAdapter, "generate_concepts")


def test_mock_media_methods_keep_domain_trace_markers():
    from orchestrator.adapters.mock import MockAdapter

    for method in ("build_creator", "generate_clip", "qc_check", "assemble"):
        assert _trace_name(getattr(MockAdapter, method)).startswith("adapter.mock.")


def test_concrete_domain_adapters_keep_trace_markers():
    from orchestrator.adapters.creator_real import RealCreatorAdapter
    from orchestrator.adapters.elevenlabs_voice import ElevenLabsVoiceAdapter
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.adapters.openai_image import OpenAIImageAdapter
    from orchestrator.adapters.replicate_upscale import ReplicateUpscaleAdapter
    from orchestrator.adapters.replicate_video import ReplicateVideoAdapter
    from orchestrator.adapters.replicate_voice import ReplicateVoiceAdapter
    from orchestrator.adapters.topaz_upscale import TopazUpscaleAdapter
    from orchestrator.adapters.vercel_gateway_video import VercelGatewayVideoAdapter

    methods = (
        (MockAdapter, "generate_clip"),
        (MockAdapter, "build_creator"),
        (RealCreatorAdapter, "build_creator"),
        (OpenAIImageAdapter, "generate_face"),
        (TopazUpscaleAdapter, "upscale"),
        (ElevenLabsVoiceAdapter, "create_voice"),
        (ReplicateVideoAdapter, "generate_clip"),
        (VercelGatewayVideoAdapter, "generate_clip"),
        (ReplicateUpscaleAdapter, "upscale"),
        (ReplicateVoiceAdapter, "create_voice"),
    )
    assert all(_trace_name(getattr(cls, method)).startswith("adapter.") for cls, method in methods)
    assert MockAdapter.generate_clip.__trace_metadata__["step"] == "video"


def test_language_runtime_marks_native_agent_backend(monkeypatch):
    from orchestrator import language_runtime
    observed: dict[str, object] = {}
    monkeypatch.setattr(language_runtime, "add_trace_metadata", lambda **values: observed.update(values))
    runtime = language_runtime.LanguageRuntime.from_provider("mock", {})

    async def materialize(submission):
        return submission

    import asyncio

    asyncio.run(
        runtime.run_agent(
            stage="concepts",
            inputs={"offer": "x", "n": 1},
            materialize=materialize,
        )
    )
    assert observed == {"agent_backend": "langchain", "stage": "concepts", "provider": "mock"}
