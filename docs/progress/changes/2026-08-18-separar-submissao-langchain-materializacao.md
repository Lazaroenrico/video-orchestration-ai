# Separar submissão LangChain da materialização de criativos

Data: `2026-08-18`

## Resultado

Desacoplamento completo entre a geração estruturada do LangChain (`LanguageRuntime.generate_structured`) e a materialização de regras de domínio server-owned no executor (`CreativeStageExecutor`). O `LanguageRuntime` retorna estritamente modelos Pydantic validados dos esquemas criativos (`creative-v2`), e o executor aplica a chamada tipada da ferramenta (`tool_fn`) com dados confiáveis (`agent_submission=True`) sem callbacks acoplados.

## Mudanças de contrato

- `LanguageRuntime.run_agent(..., materialize=...)` foi substituído por `LanguageRuntime.generate_structured(self, *, stage: str, inputs: dict[str, Any], system_prompt: str | None = None, model: str | None = None) -> BaseModel`.
- `stage_executor._execute_agentic_tool` consome diretamente `runtime.generate_structured`, valida o schema Pydantic e invoca a ferramenta tipada de materialização com os campos sanitizados e server-owned.
- Nenhuma alteração nos schemas públicos de API V2 ou nós do grafo LangGraph.

## RED → GREEN

- **RED:** `tests/test_language_runtime.py`, `tests/test_stage_executor.py` e `tests/test_tracing_coverage.py` ajustados para invocar `generate_structured` e esperar retorno `BaseModel` sem callback `materialize`; falha inicial com `AttributeError: 'LanguageRuntime' object has no attribute 'generate_structured'`.
- **GREEN:** Implementação de `generate_structured` em `src/orchestrator/language_runtime.py` e refatoração de `_execute_agentic_tool` em `src/orchestrator/stage_executor.py` para materialização direta pós-validação de schema.
- **REFACTOR:** Adição de testes de borda para tipos de resposta inesperados, propagação de `ValidationError` e eliminação de fallback defensivo de dicionário no executor, aderindo estritamente ao retorno `BaseModel`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `ImportError: cannot import name 'ConceptProposalsSubmission' from 'orchestrator.creative_contracts'` | O modelo de submissão do estágio `concepts` no runtime chama-se `ConceptAgentOutput` em `orchestrator.language_runtime`. | Corrigido o import no teste unitário para importar `ConceptAgentOutput`. |
| `I001 / F401` violações de lint no Ruff | Imports não utilizados e fora de ordem nos blocos de teste de exceção. | Reorganizados imports e removidas variáveis não utilizadas via `ruff check --fix`. |

## Verificação final

- `pytest tests/test_language_runtime.py tests/test_stage_executor.py tests/test_creative_agent_tools.py tests/test_agent_prompt_security.py tests/test_creative_contracts.py tests/test_tracing_coverage.py`: 66 testes aprovados (100% de cobertura em `stage_executor.py` e 89% em `language_runtime.py`).
- `pytest tests/test_builder.py tests/test_graph_e2e.py tests/test_dev_local.py tests/test_staging_contract.py tests/test_live_config_no_mock.py tests/test_cli.py tests/test_agent_catalog.py tests/test_adapters_mock.py`: 119 testes aprovados.
- `ruff check src tests`: 0 erros, código 100% conforme.

## Pendências ou bloqueios externos

Nenhum.
