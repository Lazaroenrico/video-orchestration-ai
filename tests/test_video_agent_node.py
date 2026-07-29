"""Video nodes stay deterministic adapter calls outside agent authority."""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.adapters._agent_loop import AgentRunResult
from orchestrator.adapters.mock import MockAdapter
from orchestrator.agent_catalog import AgentCatalog, StageExecutionSpec
from orchestrator.graph.state import Item

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


class _MultiTakeAdapter(MockAdapter):
    """A trap adapter: its agent entrypoint must never be reached by video nodes."""

    def __init__(self, takes: int = 3) -> None:
        super().__init__(tiers=TIERS)
        self.takes = takes

    async def run_stage_agent(
        self,
        *,
        stage: str,
        allowed_tools: tuple[str, ...],
        run_tool: Any,
        inputs: dict[str, Any],
        target_model: Any = None,
        system_prompt: str | None = None,
        max_steps: int = 4,
        max_tool_calls: int | None = None,
    ) -> AgentRunResult:
        raise AssertionError("video must not enter the agent loop")


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

    adapter = _MultiTakeAdapter(takes=3)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "tool"))

    single = out["clips"][-1].meta["cost_usd"]
    assert out["cost_usd"] == pytest.approx(single)


@pytest.mark.parametrize("node_name", ["gen", "product_demo"])
async def test_video_node_appends_one_adapter_result(pipeline_cfg, node_name):
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    adapter = _MultiTakeAdapter(takes=3)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "tool"))

    assert len(out["clips"]) == 1


async def test_video_node_does_not_create_agent_take_provenance(pipeline_cfg):
    from orchestrator.nodes.stages import make_gen_node

    adapter = _MultiTakeAdapter(takes=3)
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

    adapter = _MultiTakeAdapter(takes=1)
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

    adapter = MockAdapter(tiers=TIERS)  # sem run_stage_agent → modo tool
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
