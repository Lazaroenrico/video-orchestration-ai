"""Loop de tool-calling compartilhado pelos adapters LLM message-based (D32/D33).

Centraliza os invariantes do agent execution num só lugar (como ``is_agent_stage_allowed``
faz para o gating de stage):

- **Budget**: no máximo ``max_steps`` rodadas de decisão do modelo — nunca loop infinito.
  ``max_tool_calls`` capa as *chamadas de tool*: um único step pode pedir N delas, e em
  mídia cada uma custa dinheiro (D33).
- **Fronteira D29**: o loop só toca o domínio via ``run_tool`` (a typed tool validada);
  nunca chama ``generate_concepts``/``write_script`` diretamente.
- **Allowlist**: um ``tool_call`` fora de ``allowed_tools`` não roda — o erro volta ao
  modelo e o loop segue.
- **Erros como feedback (D33)**: se a tool levanta, o erro vira tool_result e volta ao
  modelo, que pode ajustar os args e tentar de novo dentro do budget. Se o budget acabar
  sem nenhum sucesso, o último erro **propaga** — o stage nunca retorna sucesso falso.
- **Safety-net**: se o modelo terminar sem nunca chamar uma tool, o loop roda a tool
  primária uma vez, garantindo que o stage sempre produza um output de domínio válido.

O provider-específico (OpenAI-compatible via httpx, Anthropic via SDK) vive num
``AgentBrain``: monta as mensagens iniciais, completa um step (parseando ``tool_calls``)
e formata a mensagem de resultado de tool. O loop não conhece o transporte.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from orchestrator.adapters.base import DEFAULT_MAX_STEPS, StageToolRunner

__all__ = [
    "DEFAULT_MAX_STEPS",
    "AgentBrain",
    "AgentRunResult",
    "ToolAttempt",
    "ToolCall",
    "run_agent_loop",
    "summarize_tool_result",
]

_RESULT_CHAR_BUDGET = 4000


def _elide_data_uris(value: Any) -> Any:
    """Troca data URIs por um resumo antes de devolver o resultado ao modelo.

    Artifacts de mídia carregam ``data:...;base64,<megabytes>`` (o MockAdapter gera
    exatamente isso). Mandar o payload cru queimaria o contexto do modelo com bytes que
    ele não sabe ler — o que importa é o tipo e o tamanho.
    """
    if isinstance(value, str):
        if value.startswith("data:"):
            head, _, _ = value.partition(",")
            kb = len(value) / 1024
            return f"{head} ({kb:.1f} KB, elided)"
        return value
    if isinstance(value, dict):
        return {k: _elide_data_uris(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_elide_data_uris(v) for v in value]
    return value


def summarize_tool_result(result: Any) -> str:
    """Serializa o resultado de uma tool para devolver ao modelo (elidido e truncado)."""
    try:
        payload = result
        dump = getattr(payload, "model_dump", None)
        if callable(dump):  # pydantic (Artifact/QCResult) → dict antes de elidir
            payload = dump(mode="json")
        # RecursionError: ``_elide_data_uris`` desce na estrutura, então uma referência
        # circular estoura aqui antes de o json.dumps virar ValueError.
        return json.dumps(_elide_data_uris(payload), default=str)[:_RESULT_CHAR_BUDGET]
    except (TypeError, ValueError, RecursionError):
        return repr(result)[:_RESULT_CHAR_BUDGET]


@dataclass(frozen=True)
class ToolCall:
    """Uma chamada de tool decidida pelo modelo num step."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolAttempt:
    """Uma execução de tool no loop — com o resultado OU o erro que ela levantou."""

    call: ToolCall
    result: Any = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class AgentRunResult:
    """Saída de um stage agentic: o output final + TODAS as tentativas.

    ``attempts`` existe porque uma tool de mídia custa dinheiro por chamada: o node
    precisa contabilizar as takes descartadas, não só a vencedora (D33). É um dataclass
    (e não uma tupla) para que campos futuros — tokens, latência — não quebrem os
    call-sites de novo.
    """

    result: Any
    attempts: tuple[ToolAttempt, ...] = ()

    @property
    def executed(self) -> int:
        """Quantas chamadas de tool efetivamente rodaram (observabilidade)."""
        return len(self.attempts)

    @property
    def successful(self) -> tuple[ToolAttempt, ...]:
        return tuple(a for a in self.attempts if a.ok)

    @property
    def superseded(self) -> tuple[ToolAttempt, ...]:
        """Takes bem-sucedidas que não são a final — custo real, output descartado."""
        return self.successful[:-1]


@runtime_checkable
class AgentBrain(Protocol):
    """Ponte provider-específica que o loop usa para falar com o modelo."""

    def initial_messages(
        self, stage: str, inputs: dict[str, Any], tool_schemas: list[dict[str, Any]]
    ) -> list[Any]:
        """Mensagens iniciais (formato nativo do provider) para começar o loop."""
        ...

    async def complete(
        self, messages: list[Any], tool_schemas: list[dict[str, Any]]
    ) -> tuple[Any, list[ToolCall]]:
        """Completa um step: retorna (mensagem-assistant nativa, tool_calls do modelo)."""
        ...

    def tool_result_message(self, call: ToolCall, result: Any) -> Any:
        """Formata o resultado de uma tool como mensagem nativa a devolver ao modelo."""
        ...


async def run_agent_loop(
    brain: AgentBrain,
    *,
    stage: str,
    allowed_tools: tuple[str, ...],
    run_tool: StageToolRunner,
    inputs: dict[str, Any],
    max_steps: int,
    tool_schemas: list[dict[str, Any]] | None = None,
    max_tool_calls: Optional[int] = None,
) -> AgentRunResult:
    """Roda o loop de tool-calling e retorna o output final + todas as tentativas.

    ``max_steps`` limita rodadas de decisão do modelo; ``max_tool_calls`` (``None`` = sem
    cap) limita as chamadas de tool efetivas — a guarda de custo real em mídia, já que um
    único step pode pedir várias takes.
    """
    schemas = tool_schemas if tool_schemas is not None else []
    messages = brain.initial_messages(stage, inputs, schemas)
    attempts: list[ToolAttempt] = []
    last_result: Any = None
    last_error: Optional[BaseException] = None
    had_success = False
    capped = False

    for _ in range(max(1, max_steps)):
        assistant_message, tool_calls = await brain.complete(messages, schemas)
        if not tool_calls:
            break
        messages.append(assistant_message)
        for call in tool_calls:
            if max_tool_calls is not None and len(attempts) >= max_tool_calls:
                capped = True
                break
            if call.name not in allowed_tools:
                # Fronteira D29: não roda no domínio; devolve o erro ao modelo e segue.
                messages.append(
                    brain.tool_result_message(
                        call, {"error": f"tool {call.name!r} is not allowed for stage {stage!r}"}
                    )
                )
                continue
            try:
                result = await run_tool(call.name, **call.arguments)
            except Exception as exc:  # noqa: BLE001 - o erro vira feedback para o modelo
                # D33: a tool falhou (provider fora, tier sem adapter real, arg inválido).
                # Devolve o erro ao modelo para ele ajustar e tentar de novo DENTRO do
                # budget; se acabar sem nenhum sucesso, propaga lá embaixo. ``Exception``
                # (não ``BaseException``) para CancelledError/KeyboardInterrupt subirem.
                last_error = exc
                attempts.append(ToolAttempt(call=call, error=exc))
                messages.append(
                    brain.tool_result_message(call, {"error": f"{type(exc).__name__}: {exc}"})
                )
                continue
            last_result = result
            had_success = True
            attempts.append(ToolAttempt(call=call, result=result))
            messages.append(brain.tool_result_message(call, result))
        if capped:
            break

    if not had_success:
        if last_error is not None:
            # Toda tentativa falhou: propaga o erro real em vez de sucesso falso. Não roda
            # a safety-net — seria mais uma chamada paga fadada ao mesmo erro.
            raise last_error
        # Safety-net: o modelo nunca chamou uma tool válida — garante output de domínio.
        # Sem try/except: se esta falhar, o stage falha, que é o correto.
        call = ToolCall(id="safety-net", name=allowed_tools[0])
        last_result = await run_tool(call.name)
        attempts.append(ToolAttempt(call=call, result=last_result))

    return AgentRunResult(result=last_result, attempts=tuple(attempts))
