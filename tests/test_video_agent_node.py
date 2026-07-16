"""Nodes de vídeo em modo agent (D33): contabilidade de custo por take.

O agent pode gerar várias takes para um mesmo clip, e **cada take é dinheiro** (Replicate
cobra por geração). O node só anexa a take final ao item — mas precisa cobrar por todas,
senão o custo do run mente. Estes testes travam essa contabilidade e a proveniência das
takes descartadas.

Offline e determinístico: um adapter fake implementa ``run_stage_agent`` chamando o
``run_tool`` recebido N vezes, sem rede.
"""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.adapters._agent_loop import AgentRunResult, ToolAttempt, ToolCall
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
    """Adapter agentic que pede ``takes`` gerações, cada uma com uma revision distinta.

    Simula o agent refinando a take: a última é a vencedora, as anteriores são custo
    pago e descartado.
    """

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
        attempts: list[ToolAttempt] = []
        for i in range(self.takes):
            revision = f"take-{i}" if i else None
            kwargs = {"revision": revision} if revision else {}
            result = await run_tool("generate_clip", **kwargs)
            attempts.append(
                ToolAttempt(
                    call=ToolCall(id=str(i), name="generate_clip", arguments=dict(kwargs)),
                    result=result,
                )
            )
        return AgentRunResult(result=attempts[-1].result, attempts=tuple(attempts))


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
async def test_video_node_charges_every_agent_take(pipeline_cfg, node_name):
    """O custo do item soma TODAS as takes — não só a que sobreviveu."""
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    adapter = _MultiTakeAdapter(takes=3)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "agent"))

    # 3 takes de mesmo custo unitário; o node cobra as 3, não 1.
    single = out["clips"][-1].meta["cost_usd"]
    assert out["cost_usd"] == pytest.approx(round(single * 3, 4))


@pytest.mark.parametrize("node_name", ["gen", "product_demo"])
async def test_video_node_appends_only_the_final_take(pipeline_cfg, node_name):
    """Só a take final vira clip do item.

    Empurrar as descartadas para ``clips`` reprovaria o item no IntegrityQC (que valida
    cada clip) e quebraria ``qc.required_clip_count``.
    """
    from orchestrator.nodes.stages import make_gen_node, node_product_demo

    adapter = _MultiTakeAdapter(takes=3)
    node = make_gen_node("ltx") if node_name == "gen" else node_product_demo
    out = await node(_item().model_dump(), _config(adapter, pipeline_cfg, "agent"))

    assert len(out["clips"]) == 1


async def test_video_node_records_superseded_takes_as_provenance(pipeline_cfg):
    """As takes descartadas ficam no meta do clip final: custo e revision auditáveis."""
    from orchestrator.nodes.stages import make_gen_node

    adapter = _MultiTakeAdapter(takes=3)
    out = await make_gen_node("ltx")(_item().model_dump(), _config(adapter, pipeline_cfg, "agent"))

    meta = out["clips"][-1].meta
    assert meta["agent_takes"] == 3
    superseded = meta["superseded_takes"]
    assert len(superseded) == 2
    assert [t["revision"] for t in superseded] == [None, "take-1"]
    assert all(isinstance(t["cost_usd"], float) for t in superseded)


async def test_video_node_single_take_has_no_superseded_metadata(pipeline_cfg):
    """Uma take só (o caso comum) não polui o meta do clip."""
    from orchestrator.nodes.stages import make_gen_node

    adapter = _MultiTakeAdapter(takes=1)
    out = await make_gen_node("ltx")(_item().model_dump(), _config(adapter, pipeline_cfg, "agent"))

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


async def test_video_node_propagates_a_failed_agent_run(pipeline_cfg):
    """Agent que falha derruba o node — o item não pode ficar inconsistente."""
    from orchestrator.nodes.stages import make_gen_node

    class _FailingAdapter(MockAdapter):
        async def run_stage_agent(self, **kwargs: Any) -> AgentRunResult:
            raise RuntimeError("tier has no real adapter")

    adapter = _FailingAdapter(tiers=TIERS)
    with pytest.raises(RuntimeError, match="tier has no real adapter"):
        await make_gen_node("ltx")(_item().model_dump(), _config(adapter, pipeline_cfg, "agent"))
