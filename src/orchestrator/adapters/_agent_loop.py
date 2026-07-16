"""Loop de tool-calling compartilhado pelos adapters LLM message-based (Fase 1).

Centraliza os invariantes do agent execution num só lugar (como ``is_agent_stage_allowed``
faz para o gating de stage):

- **Budget**: no máximo ``max_steps`` rodadas de decisão do modelo — nunca loop infinito.
- **Fronteira D29**: o loop só toca o domínio via ``run_tool`` (a typed tool validada);
  nunca chama ``generate_concepts``/``write_script`` diretamente.
- **Allowlist**: um ``tool_call`` fora de ``allowed_tools`` não roda — o erro volta ao
  modelo e o loop segue.
- **Safety-net**: se o modelo terminar sem nunca chamar uma tool, o loop roda a tool
  primária uma vez, garantindo que o stage sempre produza um output de domínio válido.

O provider-específico (OpenAI-compatible via httpx, Anthropic via SDK) vive num
``AgentBrain``: monta as mensagens iniciais, completa um step (parseando ``tool_calls``)
e formata a mensagem de resultado de tool. O loop não conhece o transporte.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Protocol, runtime_checkable

from orchestrator.adapters.base import StageToolRunner

# Budget default de rodadas de decisão do modelo por stage agentic. Sobrescrevível
# via ``agent.max_steps`` no pipeline.yaml (lido pelo stage_executor).
DEFAULT_MAX_STEPS = 4


@dataclass(frozen=True)
class ToolCall:
    """Uma chamada de tool decidida pelo modelo num step."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


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
) -> tuple[Any, int]:
    """Roda o loop de tool-calling e retorna ``(resultado_final, tools_executadas)``.

    ``resultado_final`` é a saída da última tool executada (o output de domínio do stage).
    ``tools_executadas`` conta quantas chamadas de tool efetivamente rodaram (observabilidade).
    """
    schemas = tool_schemas if tool_schemas is not None else []
    messages = brain.initial_messages(stage, inputs, schemas)
    last_result: Any = None
    executed = 0

    for _ in range(max(1, max_steps)):
        assistant_message, tool_calls = await brain.complete(messages, schemas)
        if not tool_calls:
            break
        messages.append(assistant_message)
        for call in tool_calls:
            if call.name not in allowed_tools:
                # Fronteira D29: não roda no domínio; devolve o erro ao modelo e segue.
                messages.append(
                    brain.tool_result_message(
                        call, {"error": f"tool {call.name!r} is not allowed for stage {stage!r}"}
                    )
                )
                continue
            result = await run_tool(call.name, **call.arguments)
            last_result = result
            executed += 1
            messages.append(brain.tool_result_message(call, result))

    if last_result is None:
        # Safety-net: o modelo nunca chamou uma tool válida — garante output de domínio.
        last_result = await run_tool(allowed_tools[0])
        executed += 1

    return last_result, executed
