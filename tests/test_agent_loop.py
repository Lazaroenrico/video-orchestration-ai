"""Loop de tool-calling compartilhado (Fase 1) — provider-agnostic.

O loop centraliza os invariantes: budget (max_steps), fronteira D29 (só toca o domínio
via ``run_tool``), enforcement de ``allowed_tools`` e a safety-net que garante ao menos
um output de domínio válido. Um ``AgentBrain`` fake (sem rede) fornece as decisões.
"""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.adapters._agent_loop import (
    AgentBrain,
    AgentRunResult,
    ToolAttempt,
    ToolCall,
    run_agent_loop,
)


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


def _tool_result_messages(brain: _FakeBrain) -> list[Any]:
    """As mensagens de tool result que o brain viu na última completude."""
    if not brain.seen_messages:
        return []
    return [m for m in brain.seen_messages[-1] if m.get("role") == "tool"]


async def test_agent_loop_single_tool_call_then_stop():
    """Modelo chama a tool uma vez e finaliza (sem tool_call no 2º step)."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain([[ToolCall(id="1", name="generate_concepts", arguments={})], []])

    run = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert run.result == "draft"
    assert run.executed == 1
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

    run = await run_agent_loop(
        brain,
        stage="scripts",
        allowed_tools=("write_script",),
        run_tool=run_tool,
        inputs={"concept": {"id": "c"}},
        max_steps=4,
    )

    assert run.result == "draft/tighten CTA"
    assert run.executed == 2
    assert calls[1] == ("write_script", {"revision": "tighten CTA"})


async def test_agent_loop_respects_step_budget():
    """Modelo que nunca para é cortado no budget; retorna o último resultado válido."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    # cada step pede mais uma tool call — infinito sem o budget.
    brain = _FakeBrain([[ToolCall(id=str(i), name="generate_concepts", arguments={})] for i in range(10)])

    run = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=3,
    )

    assert run.executed == 3  # cortado no budget, não 10
    assert run.result == "draft"


async def test_agent_loop_safety_net_runs_tool_when_model_never_calls():
    """Se o modelo nunca chama tool, a safety-net roda a tool primária uma vez.

    Garante que o stage sempre produz um output de domínio válido.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain([[]])  # modelo responde sem tool_call de cara

    run = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert run.result == "draft"
    assert run.executed == 1
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

    run = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={"offer": "o"},
        max_steps=4,
    )

    assert ("delete_everything", {}) not in calls
    assert calls == [("generate_concepts", {})]
    assert run.result == "draft"
    assert run.executed == 1


def test_agent_brain_is_runtime_checkable_protocol():
    assert isinstance(_FakeBrain([]), AgentBrain)


# --------------------------------------------------------------------------- #
# D33 — tentativas, erros e budget de chamadas (custo de mídia por take)       #
# --------------------------------------------------------------------------- #


async def test_agent_loop_records_every_attempt_with_its_call():
    """``attempts`` guarda cada execução com o ToolCall que a originou.

    O node de mídia contabiliza custo por take, então precisa de todas — não só da final.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [
            [ToolCall(id="1", name="generate_clip", arguments={})],
            [ToolCall(id="2", name="generate_clip", arguments={"revision": "tighter"})],
            [],
        ]
    )

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={"item_id": "i1"},
        max_steps=4,
    )

    assert [a.result for a in run.attempts] == ["draft", "draft/tighter"]
    assert run.attempts[1].call.arguments == {"revision": "tighter"}
    assert all(a.ok for a in run.attempts)


async def test_agent_loop_superseded_excludes_the_final_take():
    """``superseded`` = takes pagas cujo output foi descartado (a final não entra)."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [
            [ToolCall(id="1", name="generate_clip", arguments={})],
            [ToolCall(id="2", name="generate_clip", arguments={"revision": "a"})],
            [ToolCall(id="3", name="generate_clip", arguments={"revision": "b"})],
            [],
        ]
    )

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
    )

    assert len(run.successful) == 3
    assert [a.result for a in run.superseded] == ["draft", "draft/a"]
    assert run.result == "draft/b"
    assert run.result not in [a.result for a in run.superseded]


async def test_agent_loop_returns_tool_error_to_the_model_and_retries():
    """Falha da tool vira tool_result de erro; o modelo ajusta e tenta de novo.

    É o valor de agentificar mídia: reagir ao caminho de falha dentro do budget.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        calls.append((tool_name, kwargs))
        if "revision" not in kwargs:
            raise RuntimeError("provider exploded")
        return "clip"

    brain = _FakeBrain(
        [
            [ToolCall(id="1", name="generate_clip", arguments={})],
            [ToolCall(id="2", name="generate_clip", arguments={"revision": "retry"})],
            [],
        ]
    )

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
    )

    assert run.result == "clip"
    assert run.attempts[0].ok is False
    assert isinstance(run.attempts[0].error, RuntimeError)
    assert run.successful == (run.attempts[1],)
    # O modelo precisa ter visto o erro para poder corrigir.
    errors = [m["result"] for m in _tool_result_messages(brain) if "error" in m["result"]]
    assert errors == [{"error": "RuntimeError: provider exploded"}]


async def test_agent_loop_raises_last_error_when_budget_ends_without_success():
    """Se toda tentativa falhou, o erro real propaga — nunca sucesso falso.

    E a safety-net NÃO roda: seria mais uma chamada paga fadada ao mesmo erro.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        calls.append((tool_name, kwargs))
        raise RuntimeError("tier has no real adapter")

    brain = _FakeBrain([[ToolCall(id=str(i), name="generate_clip", arguments={})] for i in range(10)])

    with pytest.raises(RuntimeError, match="tier has no real adapter"):
        await run_agent_loop(
            brain,
            stage="video",
            allowed_tools=("generate_clip",),
            run_tool=run_tool,
            inputs={},
            max_steps=2,
        )

    assert len(calls) == 2  # cortado no budget; safety-net não adicionou uma 3ª


async def test_agent_loop_does_not_call_safety_net_when_a_tool_returned_none():
    """Tool que legitimamente retorna ``None`` não dispara a safety-net.

    Antes do D33 o sentinela era ``last_result is None``, que confundia "nunca rodou"
    com "rodou e retornou None" — e gerava uma segunda chamada paga invisível.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        calls.append((tool_name, kwargs))
        return None

    brain = _FakeBrain([[ToolCall(id="1", name="generate_clip", arguments={})], []])

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
    )

    assert len(calls) == 1
    assert run.result is None
    assert run.executed == 1


async def test_agent_loop_propagates_a_safety_net_failure():
    """Safety-net que falha derruba o stage — o correto: não há output de domínio."""

    async def run_tool(tool_name: str, **kwargs: Any) -> Any:
        raise RuntimeError("primary tool down")

    brain = _FakeBrain([[]])  # modelo nunca chama tool

    with pytest.raises(RuntimeError, match="primary tool down"):
        await run_agent_loop(
            brain,
            stage="video",
            allowed_tools=("generate_clip",),
            run_tool=run_tool,
            inputs={},
            max_steps=4,
        )


async def test_agent_loop_safety_net_attempt_is_recorded():
    """A chamada da safety-net também é uma tentativa (custa dinheiro em mídia)."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain([[]])

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
    )

    assert run.attempts[0].call.id == "safety-net"
    assert run.attempts[0].result == "draft"


async def test_agent_loop_caps_tool_calls_within_a_single_step():
    """``max_tool_calls`` é a guarda de dinheiro: max_steps conta rodadas, não calls.

    Um único step pode emitir N tool_calls — sem cap, N takes pagas num "budget de 1".
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [[ToolCall(id=str(i), name="generate_clip", arguments={}) for i in range(5)], []]
    )

    run = await run_agent_loop(
        brain,
        stage="video",
        allowed_tools=("generate_clip",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
        max_tool_calls=2,
    )

    assert len(calls) == 2  # 5 pedidas, 2 executadas
    assert run.executed == 2


async def test_agent_loop_without_a_call_cap_runs_every_requested_call():
    """``max_tool_calls=None`` (default) não capa nada — texto segue inalterado."""
    calls: list[tuple[str, dict[str, Any]]] = []
    run_tool = await _collecting_run_tool(calls)
    brain = _FakeBrain(
        [[ToolCall(id=str(i), name="generate_concepts", arguments={}) for i in range(3)], []]
    )

    run = await run_agent_loop(
        brain,
        stage="concepts",
        allowed_tools=("generate_concepts",),
        run_tool=run_tool,
        inputs={},
        max_steps=4,
    )

    assert len(calls) == 3
    assert run.executed == 3


def test_tool_attempt_ok_reflects_the_error():
    call = ToolCall(id="1", name="generate_clip")
    assert ToolAttempt(call=call, result="x").ok is True
    assert ToolAttempt(call=call, error=RuntimeError("boom")).ok is False


def test_agent_run_result_defaults_to_no_attempts():
    run = AgentRunResult(result="x")
    assert run.attempts == ()
    assert run.executed == 0
    assert run.successful == ()
    assert run.superseded == ()


# --------------------------------------------------------------------------- #
# D33 — serialização do resultado devolvido ao modelo                          #
# --------------------------------------------------------------------------- #


def test_summarize_tool_result_elides_data_uris():
    """Um Artifact de mídia carrega data URI base64 — devolvê-lo cru queima contexto.

    O MockAdapter gera exatamente isso (_svg_data_uri/_wav_data_uri), então o caminho
    offline também depende disto.
    """
    from orchestrator.adapters._agent_loop import summarize_tool_result

    payload = {"kind": "clip", "uri": "data:video/mp4;base64," + "A" * 5000}
    out = summarize_tool_result(payload)

    assert "AAAA" not in out
    assert "data:video/mp4;base64" in out  # o modelo ainda sabe o que é
    assert "elided" in out
    assert len(out) < 500


def test_summarize_tool_result_elides_data_uri_inside_a_pydantic_artifact():
    from orchestrator.adapters._agent_loop import summarize_tool_result
    from orchestrator.graph.state import Artifact

    art = Artifact(kind="clip", uri="data:image/svg+xml;base64," + "B" * 3000,
                   meta={"cost_usd": 0.08})
    out = summarize_tool_result(art)

    assert "BBBB" not in out
    assert "cost_usd" in out  # a meta útil sobrevive


def test_summarize_tool_result_keeps_a_normal_uri():
    from orchestrator.adapters._agent_loop import summarize_tool_result

    out = summarize_tool_result({"uri": "https://cdn/clip.mp4"})

    assert "https://cdn/clip.mp4" in out


def test_summarize_tool_result_falls_back_on_unserializable():
    from orchestrator.adapters._agent_loop import summarize_tool_result

    circular: dict[str, Any] = {}
    circular["self"] = circular

    assert summarize_tool_result(circular)  # não levanta
