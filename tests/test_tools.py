"""Contract tests for the node -> tools -> adapters layer."""
from __future__ import annotations

import re
from typing import Any

import pytest

from orchestrator.adapters.base import VoiceProfile
from orchestrator.graph.state import Artifact, QCResult, new_item


def _config(adapter: Any, *, pipeline: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": pipeline or {"clip": {"duration_seconds": 8}},
            "run": {"platform": "reels"},
            "thread_id": "run-tools",
        }
    }


class _SpyAdapter:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate_concepts(self, **kwargs: Any) -> Any:
        self.calls.append(("generate_concepts", kwargs))
        return self.output

    async def write_script(self, **kwargs: Any) -> Any:
        self.calls.append(("write_script", kwargs))
        return self.output

    async def build_creator(self, **kwargs: Any) -> Any:
        self.calls.append(("build_creator", kwargs))
        return self.output

    async def generate_clip(self, **kwargs: Any) -> Any:
        self.calls.append(("generate_clip", kwargs))
        return self.output

    async def qc_check(self, **kwargs: Any) -> Any:
        self.calls.append(("qc_check", kwargs))
        return self.output

    async def assemble(self, **kwargs: Any) -> Any:
        self.calls.append(("assemble", kwargs))
        return self.output

    async def upscale(self, media_uri: str) -> Any:
        self.calls.append(("upscale", {"media_uri": media_uri}))
        return self.output


def test_tool_context_from_config_extracts_runtime_values():
    from orchestrator.tools.base import tool_context_from_config

    adapter = object()
    cfg = _config(adapter, pipeline={"tiers": []})

    ctx = tool_context_from_config(cfg)

    assert ctx.adapter is adapter
    assert ctx.pipeline == {"tiers": []}
    assert ctx.run == {"platform": "reels"}
    assert ctx.run_id == "run-tools"


async def test_generate_concepts_tool_delegates_and_validates_output():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.concepts import generate_concepts_tool

    adapter = _SpyAdapter([{"id": "concept-1", "hook": "h"}])
    ctx = tool_context_from_config(_config(adapter))

    result = await generate_concepts_tool(
        ctx, offer="serum", n=1, seed="run-tools", bias=["problem"]
    )

    assert result == [{"id": "concept-1", "hook": "h"}]
    assert adapter.calls == [
        (
            "generate_concepts",
            {
                "offer": "serum",
                "n": 1,
                "seed": "run-tools",
                "bias": ["problem"],
                "revision": None,
            },
        )
    ]


async def test_write_script_tool_delegates_and_requires_non_empty_script():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.scripts import write_script_tool

    adapter = _SpyAdapter("HOOK: h\nCTA: buy")
    ctx = tool_context_from_config(_config(adapter))
    concept = {"id": "concept-1", "hook": "h"}

    result = await write_script_tool(
        ctx, concept=concept, creator_ref="creator-0", platform="tiktok"
    )

    assert result == "HOOK: h\nCTA: buy"
    assert adapter.calls == [
        (
            "write_script",
            {
                "concept": concept,
                "creator_ref": "creator-0",
                "platform": "tiktok",
                "revision": None,
            },
        )
    ]


async def test_build_creator_tool_delegates_with_voice_profile():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.creators import build_creator_tool

    profile = VoiceProfile(preset="female", prompt="warm")
    creator = {"id": "creator-0", "upscaled_base": "mock://image", "voice_id": "voice"}
    adapter = _SpyAdapter(creator)
    ctx = tool_context_from_config(_config(adapter))

    result = await build_creator_tool(
        ctx, index=0, system_prompt="creator prompt", voice_profile=profile
    )

    assert result == creator
    assert adapter.calls == [
        (
            "build_creator",
            {"index": 0, "system_prompt": "creator prompt", "voice_profile": profile},
        )
    ]


async def test_generate_clip_tool_delegates_and_returns_artifact():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.video import generate_clip_tool

    artifact = Artifact(kind="clip", uri="mock://clip", meta={"cost_usd": 0.08})
    adapter = _SpyAdapter(artifact)
    ctx = tool_context_from_config(_config(adapter))

    result = await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt="video prompt",
        reference_image_uri="data:image/png;base64,AAAA",
        stage="talking_head",
    )

    assert result == artifact
    assert adapter.calls == [
        (
            "generate_clip",
            {
                "item_id": "item-1",
                "tier": "ltx",
                "seconds": 8,
                "attempt": 1,
                "system_prompt": "video prompt",
                "reference_image_uri": "data:image/png;base64,AAAA",
            },
        )
    ]


async def test_qc_check_tool_delegates_and_returns_qc_result():
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.qc import qc_check_tool

    item = new_item({"id": "concept-1", "hook": "h"})
    qc = QCResult(passed=True, score=0.9, reasons=[])
    adapter = _SpyAdapter(qc)
    ctx = tool_context_from_config(_config(adapter))

    result = await qc_check_tool(ctx, item=item, fail_rate=0.34)

    assert result == qc
    assert adapter.calls == [("qc_check", {"item": item, "fail_rate": 0.34})]


async def test_assemble_video_tool_delegates_and_coerces_artifact_dict():
    from orchestrator.tools.assembly import assemble_video_tool
    from orchestrator.tools.base import tool_context_from_config

    item = new_item({"id": "concept-1", "hook": "h"})
    adapter = _SpyAdapter({"kind": "video", "uri": "mock://assembled", "meta": {}})
    ctx = tool_context_from_config(_config(adapter))

    result = await assemble_video_tool(
        ctx, item=item, platform="tiktok", system_prompt="assembly prompt"
    )

    assert result == Artifact(kind="video", uri="mock://assembled", meta={})
    assert adapter.calls == [
        (
            "assemble",
            {"item": item, "platform": "tiktok", "system_prompt": "assembly prompt"},
        )
    ]


async def test_upscale_video_tool_delegates_and_requires_non_empty_uri():
    from orchestrator.tools.assembly import upscale_video_tool
    from orchestrator.tools.base import tool_context_from_config

    adapter = _SpyAdapter("mock://upscaled")
    ctx = tool_context_from_config(_config(adapter))

    result = await upscale_video_tool(ctx, media_uri="mock://assembled")

    assert result == "mock://upscaled"
    assert adapter.calls == [("upscale", {"media_uri": "mock://assembled"})]


def test_require_artifact_rejects_empty_artifact_uri():
    from orchestrator.tools.base import ToolOutputError, require_artifact

    with pytest.raises(ToolOutputError, match="Artifact with non-empty uri"):
        require_artifact(Artifact(kind="clip", uri="", meta={}), tool_name="tool")


def test_require_qc_result_rejects_non_mapping_output():
    from orchestrator.tools.base import ToolOutputError, require_qc_result

    with pytest.raises(ToolOutputError, match="QCResult"):
        require_qc_result(None, tool_name="tool")


@pytest.mark.parametrize(
    ("tool_path", "function_name", "adapter_output", "kwargs", "expected_shape"),
    [
        (
            "orchestrator.tools.concepts",
            "generate_concepts_tool",
            [],
            {"offer": "o", "n": 1, "seed": "s", "bias": None},
            "non-empty list[dict",
        ),
        (
            "orchestrator.tools.scripts",
            "write_script_tool",
            "   ",
            {"concept": {"id": "c"}, "creator_ref": "creator", "platform": "tiktok"},
            "non-empty str",
        ),
        (
            "orchestrator.tools.creators",
            "build_creator_tool",
            {},
            {"index": 0, "system_prompt": None, "voice_profile": None},
            "non-empty dict",
        ),
        (
            "orchestrator.tools.video",
            "generate_clip_tool",
            {"kind": "clip"},
            {
                "item_id": "item-1",
                "tier": "ltx",
                "seconds": 8,
                "attempt": 0,
                "system_prompt": None,
                "reference_image_uri": None,
            },
            "Artifact with non-empty uri",
        ),
        (
            "orchestrator.tools.qc",
            "qc_check_tool",
            {"passed": True},
            {"item": new_item({"id": "c"}), "fail_rate": 0.34},
            "QCResult",
        ),
        (
            "orchestrator.tools.assembly",
            "assemble_video_tool",
            None,
            {"item": new_item({"id": "c"}), "platform": "tiktok", "system_prompt": None},
            "Artifact with non-empty uri",
        ),
        (
            "orchestrator.tools.assembly",
            "upscale_video_tool",
            None,
            {"media_uri": "mock://assembled"},
            "non-empty str",
        ),
    ],
)
async def test_tools_raise_clear_error_for_invalid_adapter_output(
    tool_path: str,
    function_name: str,
    adapter_output: Any,
    kwargs: dict[str, Any],
    expected_shape: str,
):
    import importlib

    from orchestrator.tools.base import ToolOutputError, tool_context_from_config

    fn = getattr(importlib.import_module(tool_path), function_name)
    ctx = tool_context_from_config(_config(_SpyAdapter(adapter_output)))

    with pytest.raises(ToolOutputError, match=function_name):
        await fn(ctx, **kwargs)
    with pytest.raises(ToolOutputError, match=re.escape(expected_shape)):
        await fn(ctx, **kwargs)


def test_tool_registry_lists_static_tool_specs():
    from orchestrator.tools.registry import TOOL_REGISTRY

    specs = {spec.name: spec for spec in TOOL_REGISTRY}

    assert set(specs) == {
        "generate_concepts",
        "write_script",
        "build_creator",
        "generate_clip",
        "qc_check",
        "assemble_video",
        "upscale_video",
    }
    assert specs["generate_concepts"].role == "llm"
    assert specs["generate_clip"].role == "video"
    assert specs["upscale_video"].stage == "upscale"


def test_tool_registry_specs_are_agent_routing_contract():
    from orchestrator.tools.registry import TOOL_REGISTRY

    names = [spec.name for spec in TOOL_REGISTRY]
    assert len(names) == len(set(names))

    for spec in TOOL_REGISTRY:
        assert spec.name
        assert spec.role
        assert spec.stage
        assert spec.description.strip()
        assert spec.function_path.startswith("orchestrator.tools.")
        assert spec.function_path.endswith(f"{spec.name}_tool")
        assert spec.target_model is None
        assert spec.target_agent is None
        assert spec.agent_enabled is False
        assert isinstance(spec.capabilities, tuple)


def test_tool_registry_resolves_functions_and_matches_trace_metadata():
    from orchestrator.tools.registry import TOOL_REGISTRY, resolve_tool_function

    for spec in TOOL_REGISTRY:
        fn = resolve_tool_function(spec)
        assert getattr(fn, "__trace_name__") == f"tool.{spec.name}"
        assert getattr(fn, "__trace_run_type__") == "tool"


def test_tool_registry_rejects_specs_without_function_path():
    from orchestrator.tools.registry import ToolSpec, resolve_tool_function

    legacy_spec = ToolSpec(
        name="legacy",
        description="Legacy four-field construction remains import-compatible.",
        role="llm",
        stage="concepts",
    )

    with pytest.raises(ValueError, match="legacy"):
        resolve_tool_function(legacy_spec)


def test_tool_registry_lookup_by_name_and_stage():
    from orchestrator.tools.registry import get_tool_spec, tool_specs_for_stage

    assert get_tool_spec("generate_concepts").stage == "concepts"
    assert [spec.name for spec in tool_specs_for_stage("concepts")] == [
        "generate_concepts"
    ]
    assert [spec.name for spec in tool_specs_for_stage("scripts")] == ["write_script"]
    assert tool_specs_for_stage("unknown") == ()

    with pytest.raises(KeyError, match="unknown_tool"):
        get_tool_spec("unknown_tool")


def test_tool_registry_exposes_agent_parameter_schemas():
    """Fase 1: cada ToolSpec declara um JSON schema dos params controláveis pelo agent.

    Os stages LLM-only (concepts/scripts) expõem ``revision`` — a única alavanca do modelo;
    offer/n/seed/etc. ficam server-authoritative (injetados pelo run_tool). Media tools
    ainda não são agentic (Fase 2), então declaram schema vazio.
    """
    from orchestrator.tools.registry import TOOL_REGISTRY, get_tool_spec

    for spec in TOOL_REGISTRY:
        assert isinstance(spec.parameters, dict)
        assert spec.parameters.get("type", "object") == "object"

    for name in ("generate_concepts", "write_script"):
        params = get_tool_spec(name).parameters
        assert "revision" in params["properties"]
        assert params["properties"]["revision"]["type"] == "string"
        # nada de params obrigatórios: o modelo pode chamar sem revisão (draft inicial).
        assert params.get("required", []) == []


def test_tool_call_schemas_builds_neutral_schema_for_allowed_tools():
    """``tool_call_schemas`` monta o contrato neutro (name/description/parameters) que os
    adapters formatam para o provider (OpenAI function-calling ou Anthropic input_schema)."""
    from orchestrator.tools.registry import tool_call_schemas

    schemas = tool_call_schemas(("generate_concepts",))
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["name"] == "generate_concepts"
    assert schema["description"].strip()
    assert schema["parameters"]["properties"]["revision"]["type"] == "string"

    # nomes desconhecidos estouram (contrato: só tools registradas viram schema).
    with pytest.raises(KeyError, match="nope"):
        tool_call_schemas(("nope",))


def test_tool_registry_covers_tool_functions_imported_by_stage_nodes():
    from orchestrator.nodes import stages
    from orchestrator.tools.registry import TOOL_REGISTRY, resolve_tool_function

    registered = {resolve_tool_function(spec) for spec in TOOL_REGISTRY}
    stage_imports = {
        value
        for name, value in vars(stages).items()
        if name.endswith("_tool")
        and callable(value)
        and getattr(value, "__module__", "").startswith("orchestrator.tools.")
    }

    assert stage_imports == registered


def test_tools_have_trace_markers():
    from orchestrator.tools.assembly import assemble_video_tool, upscale_video_tool
    from orchestrator.tools.concepts import generate_concepts_tool
    from orchestrator.tools.creators import build_creator_tool
    from orchestrator.tools.qc import qc_check_tool
    from orchestrator.tools.scripts import write_script_tool
    from orchestrator.tools.video import generate_clip_tool

    expected = {
        generate_concepts_tool: "tool.generate_concepts",
        write_script_tool: "tool.write_script",
        build_creator_tool: "tool.build_creator",
        generate_clip_tool: "tool.generate_clip",
        qc_check_tool: "tool.qc_check",
        assemble_video_tool: "tool.assemble_video",
        upscale_video_tool: "tool.upscale_video",
    }

    assert {getattr(fn, "__trace_name__") for fn in expected} == set(expected.values())
    assert all(getattr(fn, "__trace_run_type__") == "tool" for fn in expected)


async def test_stage_nodes_delegate_to_tools(monkeypatch, tmp_path):
    from orchestrator.nodes import stages
    from orchestrator.tools.base import ToolContext

    calls: list[tuple[str, dict[str, Any]]] = []
    adapter = object()
    pipeline = {
        "batch": {"default_size": 2},
        "clip": {"duration_seconds": 8},
        "qc": {"fail_rate": 0.34},
        "roster": {"creators": 1},
    }
    cfg = _config(adapter, pipeline=pipeline)

    async def concepts_tool(ctx: ToolContext, **kwargs: Any) -> list[dict[str, Any]]:
        assert ctx.adapter is adapter
        calls.append(("concepts", kwargs))
        return [{"id": "concept-1", "hook": "h"}]

    async def script_tool(ctx: ToolContext, **kwargs: Any) -> str:
        assert ctx.adapter is adapter
        calls.append(("script", kwargs))
        return "HOOK: h\nCTA: buy"

    async def creator_tool(ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        assert ctx.adapter is adapter
        calls.append(("creator", kwargs))
        return {"id": "creator-0", "upscaled_base": "mock://image", "voice_id": "voice"}

    async def clip_tool(ctx: ToolContext, **kwargs: Any) -> Artifact:
        assert ctx.adapter is adapter
        calls.append(("clip", kwargs))
        return Artifact(kind="clip", uri="mock://clip", meta={"cost_usd": 0.08})

    async def qc_tool(ctx: ToolContext, **kwargs: Any) -> QCResult:
        assert ctx.adapter is adapter
        calls.append(("qc", kwargs))
        return QCResult(passed=True, score=1.0, reasons=[])

    async def assembly_tool(ctx: ToolContext, **kwargs: Any) -> Artifact:
        assert ctx.adapter is adapter
        calls.append(("assembly", kwargs))
        return Artifact(kind="video", uri="mock://assembled", meta={})

    async def upscale_tool(ctx: ToolContext, **kwargs: Any) -> str:
        assert ctx.adapter is adapter
        calls.append(("upscale", kwargs))
        return "mock://upscaled"

    monkeypatch.setattr(stages, "generate_concepts_tool", concepts_tool)
    monkeypatch.setattr(stages, "write_script_tool", script_tool)
    monkeypatch.setattr(stages, "build_creator_tool", creator_tool)
    monkeypatch.setattr(stages, "generate_clip_tool", clip_tool)
    monkeypatch.setattr(stages, "qc_check_tool", qc_tool)
    monkeypatch.setattr(stages, "assemble_video_tool", assembly_tool)
    monkeypatch.setattr(stages, "upscale_video_tool", upscale_tool)
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)

    concepts = await stages.node_concepts(
        {"run_id": "run-tools", "config": {"offer": "serum", "batch_size": 1}}, cfg
    )
    scripts = await stages.node_scripts(concepts, cfg)
    roster = await stages.node_roster({}, cfg)
    item = new_item({"id": "concept-1", "hook": "h"})
    gen = await stages.make_gen_node("ltx")(item, cfg)
    demo = await stages.node_product_demo(item, cfg)
    qc = await stages.node_qc(item, cfg)
    assembled = await stages.node_assembly(item, cfg)
    upscaled = await stages.node_upscale(
        item.model_copy(
            update={"assembled": Artifact(kind="video", uri="mock://assembled", meta={})}
        ),
        cfg,
    )

    assert scripts["concepts"][0]["script"] == "HOOK: h\nCTA: buy"
    assert roster["roster"][0]["id"] == "creator-0"
    assert gen["clips"][0].uri == "mock://clip"
    assert demo["clips"][0].uri == "mock://clip"
    assert qc["qc"].passed is True
    assert assembled["assembled"].uri == "mock://assembled"
    assert upscaled["assembled"].uri == "mock://upscaled"
    assert [name for name, _ in calls] == [
        "concepts",
        "script",
        "creator",
        "clip",
        "clip",
        "qc",
        "assembly",
        "upscale",
    ]
