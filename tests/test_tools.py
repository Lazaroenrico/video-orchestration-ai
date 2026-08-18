"""Contract tests for the node -> tools -> adapters layer."""
from __future__ import annotations

import re
from typing import Any

import pytest

from orchestrator.adapters.base import RenderedMedia, VoiceProfile
from orchestrator.graph.state import Artifact, QCResult, new_item


def _config(
    adapter: Any,
    *,
    pipeline: dict[str, Any] | None = None,
    language_runtime: Any | None = None,
) -> dict[str, Any]:
    return {
        "configurable": {
            "adapter": adapter,
            "language_runtime": language_runtime,
            "pipeline": pipeline or {"clip": {"duration_seconds": 8}},
            "run": {"platform": "reels"},
            "thread_id": "run-tools",
        }
    }


class _SpyLanguageRuntime:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def generate_concepts(self, **kwargs: Any) -> Any:
        self.calls.append(("generate_concepts", kwargs))
        return self.output

    async def write_script(self, **kwargs: Any) -> Any:
        self.calls.append(("write_script", kwargs))
        return self.output


class _SpyAdapter:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[tuple[str, dict[str, Any]]] = []

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

    runtime = _SpyLanguageRuntime([{"id": "concept-1", "hook": "h"}])
    ctx = tool_context_from_config(_config(object(), language_runtime=runtime))

    result = await generate_concepts_tool(
        ctx, offer="serum", n=1, seed="run-tools", bias=["problem"]
    )

    assert result == [{"id": "concept-1", "hook": "h"}]
    assert runtime.calls == [
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

    runtime = _SpyLanguageRuntime("HOOK: h\nCTA: buy")
    ctx = tool_context_from_config(_config(object(), language_runtime=runtime))
    concept = {"id": "concept-1", "hook": "h"}

    result = await write_script_tool(
        ctx, concept=concept, creator_ref="creator-0", platform="tiktok"
    )

    assert result == "HOOK: h\nCTA: buy"
    assert runtime.calls == [
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
                "audio_uri": None,
                "stage": "talking_head",
            },
        )
    ]


async def _clip_prompt_for(revision: Any, *, system_prompt: Any = "video prompt") -> Any:
    """Roda a generate_clip_tool e devolve o system_prompt que chegou no adapter."""
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.video import generate_clip_tool

    adapter = _SpyAdapter(Artifact(kind="clip", uri="mock://clip", meta={"cost_usd": 0.08}))
    ctx = tool_context_from_config(_config(adapter))
    await generate_clip_tool(
        ctx,
        item_id="item-1",
        tier="ltx",
        seconds=8,
        attempt=1,
        system_prompt=system_prompt,
        revision=revision,
    )
    return adapter.calls[0][1]["system_prompt"]


async def test_generate_clip_tool_appends_revision_after_the_server_brief():
    """D33: a revision do agent é APENDADA ao brief server-authored, que segue intacto.

    O brief carrega os guardrails de ``_video_prompt`` ("No mock footage..."); o modelo
    refina a take dentro dele, nunca o revoga.
    """
    brief = "Server brief.\n\nNo mock footage. No placeholder frames."
    prompt = await _clip_prompt_for("tighter framing", system_prompt=brief)

    assert prompt.startswith(brief)  # o brief inteiro sobrevive, no início
    assert "tighter framing" in prompt
    assert prompt.index("tighter framing") > prompt.index("No mock footage")


@pytest.mark.parametrize("revision", [None, "", "   "])
async def test_generate_clip_tool_without_revision_keeps_the_prompt_untouched(revision):
    """Caminho não-agentic (e revision vazia) não pode alterar o prompt — regressão."""
    assert await _clip_prompt_for(revision) == "video prompt"


async def test_generate_clip_tool_revision_without_a_server_prompt():
    """Sem system_prompt, a diretiva vira o prompt inteiro (sem 'None' concatenado)."""
    prompt = await _clip_prompt_for("be punchier", system_prompt=None)

    assert "be punchier" in prompt
    assert "None" not in prompt


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


async def test_assemble_video_tool_preserves_rendered_media_bytes():
    from orchestrator.tools.assembly import assemble_video_tool
    from orchestrator.tools.base import tool_context_from_config

    item = new_item({"id": "concept-1", "hook": "h"})
    rendered = RenderedMedia(
        data=b"mp4",
        content_type="video/mp4",
        meta={"provider": "ffmpeg"},
    )
    adapter = _SpyAdapter(rendered)
    ctx = tool_context_from_config(_config(adapter))

    result = await assemble_video_tool(ctx, item=item, platform="tiktok")

    assert result is rendered


async def test_assemble_video_tool_rejects_empty_rendered_media():
    from orchestrator.tools.assembly import assemble_video_tool
    from orchestrator.tools.base import tool_context_from_config

    adapter = _SpyAdapter(
        RenderedMedia(data=b"", content_type="video/mp4", meta={})
    )
    ctx = tool_context_from_config(_config(adapter))

    with pytest.raises(ValueError, match="empty rendered media"):
        await assemble_video_tool(
            ctx,
            item=new_item({"id": "concept-1"}),
            platform="tiktok",
        )


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
    ctx = tool_context_from_config(
        _config(
            _SpyAdapter(adapter_output),
            language_runtime=_SpyLanguageRuntime(adapter_output),
        )
    )


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
        "design_creator_roster",
    }
    assert specs["generate_concepts"].role == "llm"
    assert specs["write_script"].stage == "scripts"
    assert specs["design_creator_roster"].terminal_submission is True


def test_tool_registry_specs_are_agent_routing_contract():
    from orchestrator.tools.registry import TOOL_REGISTRY

    names = [spec.name for spec in TOOL_REGISTRY]
    assert len(names) == len(set(names))

    for spec in TOOL_REGISTRY:
        assert spec.name
        assert spec.role
        assert spec.stage
        assert spec.description.strip()
        assert spec.terminal_submission is True


def test_tool_spec_deletion_removes_dynamic_function_path_and_dead_metadata():
    """Deletion test: ToolSpec has no function_path, target_agent, capabilities."""
    from orchestrator.tools.registry import TOOL_REGISTRY, ToolSpec

    for spec in TOOL_REGISTRY:
        assert not hasattr(spec, "function_path")
        assert not hasattr(spec, "target_agent")
        assert not hasattr(spec, "capabilities")
        assert not hasattr(spec, "target_model")
        assert not hasattr(spec, "agent_enabled")

    spec = ToolSpec(
        name="test_tool",
        description="test",
        role="llm",
        stage="concepts",
    )
    assert not hasattr(spec, "function_path")


def test_media_qc_assembly_are_direct_domain_functions_not_tools():
    """Mídia, QC, assembly, voiceover e upscale não são LangChain tools em TOOL_REGISTRY."""
    from orchestrator.tools.registry import TOOL_REGISTRY

    tool_stages = {spec.stage for spec in TOOL_REGISTRY}
    assert "video" not in tool_stages
    assert "qc" not in tool_stages
    assert "assembly" not in tool_stages
    assert "voiceover" not in tool_stages
    assert "upscale" not in tool_stages
    assert "roster" not in tool_stages


def test_tool_registry_lookup_by_name_and_stage():
    from orchestrator.tools.registry import get_tool_spec, tool_specs_for_stage

    assert get_tool_spec("generate_concepts").stage == "concepts"
    assert [spec.name for spec in tool_specs_for_stage("concepts")] == [
        "generate_concepts"
    ]
    assert [spec.name for spec in tool_specs_for_stage("scripts")] == ["write_script"]
    assert [spec.name for spec in tool_specs_for_stage("creator_profiles")] == [
        "design_creator_roster"
    ]
    assert tool_specs_for_stage("unknown") == ()

    with pytest.raises(KeyError, match="unknown_tool"):
        get_tool_spec("unknown_tool")


def test_tool_strategy_schemas_come_from_canonical_pydantic_models():
    from orchestrator.language_runtime import agent_output_model, agent_output_schema
    from orchestrator.tools.registry import TOOL_REGISTRY

    creative_stages = {"concepts", "scripts", "creator_profiles"}
    assert all(not hasattr(spec, "parameters") for spec in TOOL_REGISTRY)
    for stage in creative_stages:
        model = agent_output_model(stage)
        schema = agent_output_schema(stage)
        assert schema == model.model_json_schema()
        assert schema["additionalProperties"] is False


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
        "video": {"product_demo_tier": "seedance"},
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
    clip_calls = [kwargs for name, kwargs in calls if name == "clip"]
    assert [call["tier"] for call in clip_calls] == ["ltx", "seedance"]


async def test_voice_design_tools_delegate_to_adapter() -> None:
    from orchestrator.adapters.mock import MockAdapter
    from orchestrator.tools.base import tool_context_from_config
    from orchestrator.tools.creators import (
        derive_creator_voice_spec_tool,
        design_creator_voice_tool,
        finalize_creator_voice_tool,
    )

    adapter = MockAdapter(tiers=[{"name": "standard", "rate": 0.01}])
    cfg = _config(adapter)
    ctx = tool_context_from_config(cfg)

    profile = {
        "id": "creator-0",
        "archetype": "woman skincare reviewer",
        "visual_brief": "warm lighting bathroom",
        "voice_brief": "female warm conversational voice",
        "performance_style": "energetic upbeat",
    }

    spec = await derive_creator_voice_spec_tool(ctx, profile=profile)
    assert spec["vocal_presentation"] == "feminine"
    assert spec["energy"] == "high"

    batch = await design_creator_voice_tool(ctx, spec=spec)
    assert len(batch["candidates"]) == 3

    finalized = await finalize_creator_voice_tool(
        ctx,
        candidate_id=batch["candidates"][0]["candidate_id"],
        batch=batch,
        creator_id="creator-0",
    )
    assert finalized["selected_candidate_id"] == batch["candidates"][0]["candidate_id"]
    assert "creator-0" in finalized["voice_ref"]


async def test_generate_clip_tool_with_strict_videoport_adapter() -> None:
    from orchestrator.adapters.base import VideoPort
    from orchestrator.tools.base import ToolContext
    from orchestrator.tools.video import generate_clip_tool

    calls: list[dict[str, Any]] = []

    class StrictVideoAdapter:
        async def generate_clip(
            self,
            item_id: str,
            tier: str,
            seconds: int,
            attempt: int,
            system_prompt: str | None = None,
            reference_image_uri: str | None = None,
            audio_uri: str | None = None,
        ) -> Artifact:
            calls.append(
                {
                    "item_id": item_id,
                    "tier": tier,
                    "seconds": seconds,
                    "attempt": attempt,
                    "system_prompt": system_prompt,
                    "reference_image_uri": reference_image_uri,
                    "audio_uri": audio_uri,
                }
            )
            return Artifact(kind="clip", uri="https://example.com/clip.mp4", meta={"cost_usd": 0.05})

    adapter = StrictVideoAdapter()
    assert isinstance(adapter, VideoPort)

    ctx = ToolContext(
        adapter=adapter,
        pipeline={},
        run={},
        run_id="run-strict",
        durable=False,
    )
    artifact = await generate_clip_tool(
        ctx,
        item_id="item-strict",
        tier="ltx",
        seconds=8,
        attempt=0,
        system_prompt="Prompt text",
        reference_image_uri="https://example.com/ref.png",
        audio_uri="https://example.com/audio.wav",
        stage="talking_head",
    )

    assert artifact.kind == "clip"
    assert artifact.uri == "https://example.com/clip.mp4"
    assert len(calls) == 1
    assert calls[0]["item_id"] == "item-strict"
    assert calls[0]["audio_uri"] == "https://example.com/audio.wav"

