"""Loop de tool-calling compartilhado (Fase 1) — provider-agnostic.

O loop centraliza os invariantes: budget (max_steps), fronteira D29 (só toca o domínio
via ``run_tool``), enforcement de ``allowed_tools`` e a safety-net que garante ao menos
um output de domínio válido. Um ``AgentBrain`` fake (sem rede) fornece as decisões.
"""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.adapters._agent_loop import AgentBrain, ToolCall, run_agent_loop


class _FakeBrain:
    """Brain determinístico: devolve uma fila pré-programada de decisões por step."""

    def __init__(self, steps: list[list[ToolCall]]) -> None:
        self._steps = steps
        self.seen_messages: list[list[Any]] = []

    def initial_messages(self, stage, inputs, tool_schemas):
        return [{"role": "user", "stage": stage, "inputs": inputs}]

    async def complete(self, messages, tool_schemas):
        self.seen_messages.append(list(messages))
        idx = len(self.seen_messages) - 1
        calls = self._steps[idx] if idx < len(self._steps) else []
        return {"role": "assistant", "step": idx}, calls

    def tool_result_message(self, call, result):
        return {"role": "tool", "name": call.name, "result": result}


async def _collecting_run_tool(calls: list[tuple[str, dict[str, Any]]]):
    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        calls.append((tool_name, kwargs))
        rev = kwargs.get("revision")
        return f"draft/{rev}" if rev else "draft"

    return run_tool


async def test_agent_loop_single_tool_call_then_stop():
    """Modelo chama a tool uma vez e finaliza (sem tool_call no 2º step)."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain([[ToolCall(id="1", name="generate_concepts", arguments={})], []])

    result, executed = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert result == "draft"
    assert executed == 1
    assert calls == [("generate_concepts", {})]


async def test_agent_loop_iterates_multiple_passes():
    """Modelo itera: draft inicial, depois refina com uma revision."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [
            [ToolCall(id="1", name="write_script", arguments={})],
            [ToolCall(id="2", name="write_script", arguments={"revision": "tighten CTA"})],
            [],
        ]
    )

    result, executed = await run_agent_loop(
        brain,
        stage="scripts",
        allowed_tools=("write_script",),
        run_tool=run_tool,
        inputs={"concept": {"id": "c"}},
        max_steps=4,
    )

    assert result == "draft/tighten CTA"
    assert executed == 2
    assert calls[1] == ("write_script", {"revision": "tighten CTA"})


async def test_agent_loop_respects_step_budget():
    """Modelo que nunca para é cortado no budget; retorna o último resultado válido."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    # cada step pede mais uma tool call — infinito sem o budget.
    brain = _FakeBrain([[ToolCall(id=str(i), name="generate_concepts", arguments={})] for i in range(10)])

    result, executed = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=3,
    )

    assert executed == 3  # cortado no budget, não 10
    assert result == "draft"


async def test_agent_loop_safety_net_runs_tool_when_model_never_calls():
    """Se o modelo nunca chama tool, a safety-net roda a tool primária uma vez.

    Garante que o stage sempre produz um output de domínio válido.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain([[]])  # modelo responde sem tool_call de cara

    result, executed = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert result == "draft"
    assert executed == 1
    assert calls == [("generate_concepts", {})]


async def test_agent_loop_rejects_tool_outside_allowlist():
    """Um tool_call fora de ``allowed_tools`` não roda no domínio (fronteira D29).

    O loop registra o erro de volta ao modelo e segue; não chama run_tool para ele.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [
            [ToolCall(id="1", name="delete_everything", arguments={})],
            [ToolCall(id="2", name="generate_concepts", arguments={})],
            [],
        ]
    )

    result, executed = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert ("delete_everything", {}) not in calls
    assert calls == [("generate_concepts", {})]
    assert result == "draft"
    assert executed == 1


def test_agent_brain_is_runtime_checkable_protocol():
    assert isinstance(_FakeBrain([]), AgentBrain)
