"""Video nodes stay deterministic adapter calls outside agent authority."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.adapters.mock import MockAdapter
from orchestrator.agent_catalog import AgentCatalog, StageExecutionSpec
from orchestrator.graph.state import Item
from orchestrator.tools.video import VideoEffectError

TIERS = [{"name": "ltx", "model": "m", "cost_per_second": 0.01, "max_concurrency": 4}]


def _catalog(executor: str) -> AgentCatalog:
    return AgentCatalog(
        stages=(
            StageExecutionSpec(
                stage="video",
                executor=executor,
                tools=("generate_clip",),
                agent_enabled=executor == "agent",
            ),
        )
    )


def _item() -> Item:
    return Item(
        concept={"id": "c1", "hook": "Hook", "hook_style": "problem", "offer": "serum"},
        script="HOOK: Hook\nCTA: hoje.",
        creator_image_uri="data:image/png;base64,abc",
    )


def _config(adapter: Any, pipeline_cfg: dict[str, Any], executor: str) -> dict[str, Any]:
    return {
        "configurable": {
            "adapter": adapter,
            "pipeline": pipeline_cfg,
            "run": {},
            "thread_id": "run-1",
            "agent_catalog": _catalog(executor),
        }
    }


@pytest.mark.parametrize("node_name", ["gen", "product_demo"])
async def test_video_node_charges_the_single_adapter_take(pipeline_cfg, node_name):
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    adapter = MockAdapter(tiers=TIERS)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "tool"))

    single = out["clips"][-1].meta["cost_usd"]
    assert out["cost_usd"] == pytest.approx(single)


@pytest.mark.parametrize("node_name", ["gen", "product_demo"])
async def test_video_node_appends_one_adapter_result(pipeline_cfg, node_name):
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    adapter = MockAdapter(tiers=TIERS)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "tool"))

    assert len(out["clips"]) == 1


async def test_video_node_does_not_create_agent_take_provenance(pipeline_cfg):
    from orchestrator.nodes.stages import make_gen_node

    adapter = MockAdapter(tiers=TIERS)
    out = await make_gen_node("ltx")(
        _item().model_dump(),
        _config(adapter, pipeline_cfg, "tool"),
    )

    meta = out["clips"][-1].meta
    assert "agent_takes" not in meta
    assert "superseded_takes" not in meta


async def test_video_node_single_take_has_no_superseded_metadata(pipeline_cfg):
    """Uma take só (o caso comum) não polui o meta do clip."""
    from orchestrator.nodes.stages import make_gen_node

    adapter = MockAdapter(tiers=TIERS)
    out = await make_gen_node("ltx")(
        _item().model_dump(),
        _config(adapter, pipeline_cfg, "tool"),
    )

    meta = out["clips"][-1].meta
    assert "superseded_takes" not in meta
    assert "agent_takes" not in meta


async def test_video_node_in_tool_mode_keeps_single_take_accounting(pipeline_cfg):
    """Regressão: sem agent, o node cobra uma take e não anota proveniência."""
    from orchestrator.nodes.stages import make_gen_node

    adapter = MockAdapter(tiers=TIERS)
    out = await make_gen_node("ltx")(_item().model_dump(), _config(adapter, pipeline_cfg, "tool"))

    assert len(out["clips"]) == 1
    assert out["cost_usd"] == pytest.approx(out["clips"][-1].meta["cost_usd"])
    assert "superseded_takes" not in out["clips"][-1].meta


async def test_video_node_propagates_a_failed_adapter_call(pipeline_cfg):
    from orchestrator.nodes.stages import make_gen_node

    class _FailingAdapter(MockAdapter):
        async def generate_clip(self, **kwargs: Any):
            raise RuntimeError("tier has no real adapter")

    adapter = _FailingAdapter(tiers=TIERS)
    with pytest.raises(RuntimeError, match="tier has no real adapter"):
        await make_gen_node("ltx")(
            _item().model_dump(),
            _config(adapter, pipeline_cfg, "tool"),
        )


@pytest.mark.parametrize("node_name", ["gen", "product_demo"])
async def test_video_node_converts_expected_provider_failure_to_structured_item_failure(
    pipeline_cfg,
    node_name,
):
    from orchestrator.graph.state import Artifact
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    class _EffectFailingAdapter(MockAdapter):
        async def generate_clip(self, **kwargs: Any):
            raise VideoEffectError(
                "prediction_timeout",
                retryable=False,
                uncertain=True,
                error_type="WriteTimeout",
                effect_key="video:run-1:item-1:talking_head:0:hash",
            )

    adapter = _EffectFailingAdapter(tiers=TIERS)
    item = _item()
    item.clips = [Artifact(kind="clip", uri="mock://prior", meta={})]
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo

    out = await node(item.model_dump(), _config(adapter, pipeline_cfg, "tool"))

    failure = out["failure"]
    assert out["clips"] == item.clips
    assert out["error"]
    assert failure.stage == ("talking_head" if node_name == "gen" else "product_demo")
    assert failure.type == "WriteTimeout"
    assert failure.provider == "replicate"
    assert failure.item_id == item.id
    assert failure.effect_key.startswith("video:run-1:")
    assert failure.retryable is False
    assert failure.uncertain is True


async def test_item_graph_continues_other_items_when_one_video_effect_fails(
    pipeline_cfg,
    tmp_path,
    monkeypatch,
):
    from orchestrator.graph.builder import build_item_graph
    from orchestrator.nodes import stages

    class _PartiallyFailingAdapter(MockAdapter):
        async def generate_clip(self, item_id: str, **kwargs: Any):
            if item_id == "bad":
                raise VideoEffectError(
                    "prediction_failed",
                    retryable=False,
                    uncertain=False,
                    error_type="ReplicatePredictionError",
                    effect_key="video:run-1:bad:talking_head:0:hash",
                )
            return await super().generate_clip(item_id=item_id, **kwargs)

    pipeline = {
        **pipeline_cfg,
        "qc": {"max_attempts": 1, "fail_rate": 0.0},
    }
    adapter = _PartiallyFailingAdapter(tiers=pipeline["tiers"])
    config = {
        "configurable": {
            "adapter": adapter,
            "pipeline": pipeline,
            "run": {"platform": "tiktok"},
            "thread_id": "run-1",
        }
    }
    monkeypatch.setattr(stages, "default_videos_path", lambda: tmp_path)
    app = build_item_graph(pipeline)
    bad = _item().model_copy(update={"id": "bad", "creator_voice_ref": "voice-1"})
    good = _item().model_copy(update={"id": "good", "creator_voice_ref": "voice-1"})

    failed, succeeded = await asyncio.gather(
        app.ainvoke(bad.model_dump(), config),
        app.ainvoke(good.model_dump(), config),
    )

    assert failed["error"]
    assert failed["failure"].stage == "talking_head"
    assert failed["assembled"] is None
    assert succeeded["error"] is None
    assert succeeded["assembled"] is not None
