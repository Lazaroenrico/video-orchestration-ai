# Benchmark isolado de modelos para o stage de scripts

Data: `2026-08-26`

## Resultado

Harness de benchmark (`tests/test_script_model_benchmark.py` +
`src/orchestrator/evaluation/model_benchmark.py`) que compara modelos candidatos
para o stage `scripts` usando o mesmo caminho de produção
(`LanguageRuntime.agent_for("scripts")` + prompt composto pelo catálogo), valida a
estrutura via `ScriptAgentOutput` e regras de produção (`validate_script_submission`),
e pontua com um LLM Judge fixo (`anthropic/claude-opus-5` via chat completions no gateway),
com record/replay determinístico offline em CI sobre cassettes versionados.
O relatório agrega taxa estrutural, score médio ± desvio, tokens, custo total somando candidato
e juiz (`candidate_cost + judge_cost`), e razão score/custo por modelo.

Candidatos configurados: `deepseek/deepseek-v4-pro`, `google/gemini-3.7-flash`,
`alibaba/qwen3.8-max`. CI roda determinístico sobre cassette golden sem rede/token;
a execução real é opt-in (`--live`) e gera custo.

## Mudanças de contrato

- `config-base/judge.yaml`: seção `script_model_benchmark` (candidatos, juiz,
  preços por Mtok, samples_per_case, casos e contrato do judge gateway).
- `orchestrator.creative_contracts`: função canônica `validate_script_submission` extraída para validação de duração e contagem de palavras, compartilhada entre `write_script_tool` e `structural_check`.
- `orchestrator.language_runtime`: suporte a `max_tokens` em `LanguageRuntime.model_for`, `agent_for` e `_build_model`.
- `orchestrator.evaluation.model_benchmark`:
  - `BenchmarkJudge.build_payload`: serializa o draft usando o envelope canônico `UNTRUSTED_STAGE_DATA` no HumanMessage, mantendo o system prompt limpo de dados não confiáveis.
  - `BenchmarkJudge.judge`: captura, normaliza e persiste `usage` em cassette (live/replay), expondo em `verdict.raw["usage"]`.
  - `structural_check`: valida estrutura Pydantic (`ScriptAgentOutput`) e regras de produção (min/max palavras e target duration via `validate_script_submission`).
  - `generate_draft`: aplica `max_output_tokens` diretamente no modelo/agente via `runtime.agent_for(..., max_tokens=...)`.
  - `aggregate`: calcula custo do candidato e custo do juiz por linha, computa custo total `candidate_cost + judge_cost` e `score_cost_ratio = mean_score / total_cost`.
  - `run_benchmark`: suporta modo offline determinístico (`live=False`, padrão) reproduzindo o benchmark golden completo via `tests/cassettes/script_model_benchmark_generations.json` e `script_model_benchmark.json` sem token nem rede.

## RED → GREEN

- **RED:**
  - `test_benchmark_judge_payload_formats_untrusted_stage_data`: falha se draft for concatenado sem `UNTRUSTED_STAGE_DATA`.
  - `test_structural_check_enforces_production_word_and_duration_constraints`: falha se draft exceder duração ou violar contagem de palavras.
  - `test_generate_draft_extracts_usage_and_passes_max_output_tokens`: falha se `max_output_tokens` for ignorado.
  - `test_aggregate_sums_candidate_and_judge_costs_with_distinct_prices`: falha se custo do juiz não for somado ao candidato.
  - `test_offline_golden_benchmark_replays_in_ci_without_network`: falha se não houver reprodução completa offline sem token.
- **GREEN:**
  - Implementação das 7 correções em `creative_contracts.py`, `tools/scripts.py`, `language_runtime.py`, `model_benchmark.py` e criação da fixture versionada `script_model_benchmark_generations.json`.
  - 26 testes de benchmark passando sem rede.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `PydanticUserError: MockChatModel is not fully defined` | Anotação `Optional[int]` sem import explícito em contexto Pydantic | Uso de `int | None = None` e chamada `MockChatModel.model_rebuild()` |
| `KeyError` no `model_for` ao reutilizar `_models["mock"]` em testes | Cache de modelo alterado para tupla `(model_name, max_tokens)` | Suporte a fallback de chave string e tupla em `model_for` |
| Drafts com 20 palavras falhando na nova validação de produção de 28 palavras mínimas | Fixture `VALID_DRAFT` de teste tinha contagem inferior ao mínimo canônico | Expandida `VALID_DRAFT` para 40 palavras cumprindo hook, duração e contagem |

## Verificação final

- `uv run ruff check src tests` → limpo.
- `git diff --check` → limpo.
- `pytest tests/test_script_model_benchmark.py --no-cov` → 26 passed, 2 skipped (live).
- Suíte direcionada (254 passed, 2 skipped).
- `pytest tests/test_progress_docs.py --no-cov` → validado.

## Pendências ou bloqueios externos

- Nenhum. Todas as 7 correções concluídas com testes e documentação canônica atualizados.
