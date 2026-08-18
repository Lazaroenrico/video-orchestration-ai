# Centralização de deployments LangChain em LanguageModelFactory

Data: `2026-08-17`

## Resultado

A resolução e instanciação de modelos LangChain (`mock`, `vercel_gateway_llm`, `anthropic`,
`anthropic_sdk_gateway`) foi centralizada na nova `LanguageModelFactory`. A factory
encapsula o uso de `init_chat_model` para provedores live e `MockChatModel` para dry-run
determinístico, garantindo paridade estrita de precedência de credenciais, base URLs,
model defaults, timeouts, políticas de retry e sanitização de tracing. `LanguageRuntime`
passou a delegar a instanciação para a factory, sem criar runtimes paralelos nem alterar
contratos públicos.

## Mudanças de contrato

Nenhuma alteração em contratos REST/SSE ou `BatchState`. O módulo `language_runtime` expõe
a classe `LanguageModelFactory`, e `LanguageRuntime` preserva integralmente suas interfaces
públicas (`from_provider`, `model_for`, `agent_for`, `run_agent`).

## RED → GREEN

- **RED:** `tests/test_language_runtime.py` adicionou testes direcionados de resolução de
  deployments, precedência de autenticação, base URLs, retries e sanitização de tracing
  para `LanguageModelFactory`; os 5 testes falharam com `ImportError`.
- **GREEN:** `LanguageModelFactory` implementada com descriptor de dispatch híbrido
  (`_FactoryMethod`), mapeamento server-owned de providers, `init_chat_model` e validação
  de tracing; todos os 12 testes do módulo passaram.
- **REFACTOR:** `LanguageRuntime` refatorado para delegar `_model_name` e `_build_model`
  diretamente à `LanguageModelFactory`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Nenhuma | N/A | Paridade mantida sem regressões. |

## Verificação final

- `rtk proxy uv run python -m pytest tests/test_language_runtime.py --no-cov -q`: 12 passed.
- `rtk proxy uv run python -m pytest tests/test_language_runtime.py tests/test_creative_contracts.py tests/test_stage_executor.py tests/test_tools.py tests/test_agent_catalog.py tests/test_creative_plan_graph.py tests/test_agent_prompt_security.py tests/test_tracing.py tests/test_tracing_coverage.py tests/test_system_prompt.py --no-cov -q`: 160 passed.
- `rtk proxy uv run python -m compileall -q src`: passou.
- `rtk proxy uv run ruff check src tests`: passou.

## Pendências ou bloqueios externos

Nenhum.
