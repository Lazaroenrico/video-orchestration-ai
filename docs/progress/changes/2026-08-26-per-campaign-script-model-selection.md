# Seleção de modelo por campanha para geração de roteiros (DeepSeek)

Data: `2026-08-26`

## Resultado

Permite que cada campanha (`RunRequest` e `RunV2Request` / `CampaignInput`) escolha opcionalmente o modelo de LLM usado na fase de roteiros (`scripts`), com validação server-side contra lista permitida (`allowed_models`) configurada no `agents.yaml`. Adicionado suporte e whitelist do modelo `deepseek/deepseek-v4-pro` (vencedor do benchmark de score/custo) nos perfis de configuração.

## Mudanças de contrato

- **`config-base/agents.yaml` e `config/agents.yaml`**: `allowed_models` canônico em `config-base/agents.yaml` sob o stage `scripts`, removida duplicação não intencional no overlay `config/agents.yaml`.
- **`src/orchestrator/agent_catalog.py`**:
  - `StageExecutionSpec.allowed_models: tuple[str, ...]` adicionado ao dataclass e serialização estável.
  - `build_agent_catalog()` valida e carrega `allowed_models` de cada stage.
  - Função `with_stage_model(catalog, stage, model)` cria cópia imutável do catálogo com `target_model` substituído para o stage informado com whitelist fail-closed: se o modelo não estiver em `allowed_models` (inclusive quando vazia), levanta `ValueError`.
  - Helpers `extract_script_model(*sources, script_model=None)` e `apply_script_model_override(catalog, *sources, script_model=None)` centralizam a extração com precedência explícita e aplicação server-side fail-closed.
- **`src/orchestrator/creative_contracts.py`**: `CampaignInput.script_model: str | None = Field(default=None, max_length=200)` adicionado ao schema de entrada.
- **`src/orchestrator/web/routes_runs.py`**:
  - `RunRequest.script_model: Optional[str] = None` e validação na rota `POST /api/run` e `POST /api/v2/runs` via `apply_script_model_override` (retornando `HTTPException(400)` para modelos fora da whitelist).
  - Encaminha `script_model` no payload durável e no background task do executor local.
- **`src/orchestrator/web/run_executor.py`**, **`src/orchestrator/worker.py`** e **`src/orchestrator/runner.py`**:
  - Centralizada a aplicação do override via `apply_script_model_override` cobrindo fluxos local, durável de worker e resume.
- **`front/src/api/contracts.ts`**: Atualizadas interfaces `StartRunBody` e `CampaignInput` com `script_model?: string | null;`.

## RED → GREEN

- **RED:**
  - `tests/test_agent_catalog.py`: `test_agents_yaml_parses_allowed_models`, `test_agents_yaml_rejects_invalid_allowed_models`, `test_with_stage_model_returns_new_catalog_with_overridden_target_model`, `test_with_stage_model_enforces_allowed_models_whitelist`, `test_with_stage_model_rejects_unknown_stage`, `test_with_stage_model_enforces_fail_closed_when_allowed_models_empty`, `test_extract_script_model_precedence`, `test_apply_script_model_override`.
  - `tests/test_api_v2.py`: `test_start_v2_with_allowed_script_model_enqueues_model`, `test_start_v2_with_disallowed_script_model_returns_400`.
  - `tests/test_web_endpoints.py`: `test_start_run_with_valid_script_model_enqueues_payload`, `test_start_run_with_disallowed_script_model_returns_400`, `test_local_execute_run_applies_script_model_override`.
- **GREEN:**
  - Implementado parsing, validação fail-closed e helpers centralizados em `agent_catalog.py`, `creative_contracts.py`, `routes_runs.py`, `run_executor.py`, `worker.py`, `runner.py` e `contracts.ts`.
  - Removido `allowed_models` duplicado de `config/agents.yaml`.
  - Todos os testes de catálogo, rotas e runner aprovados.

## Falhas investigadas

| Sintoma | Causa | Correção |
|---|---|---|
| `NameError: name '_RUN_REPOSITORY_UNSET' is not defined` em `routes_runs.py` | Chamada positional para `_execute_run` incluía sentinela não importada | Refatorado para chamada com keyword argument explícito `script_model=req.script_model` |
| `SyntaxError: '(' was never closed` em `worker.py` | Parêntese de fechamento ausente na linha do `RuntimeError` após edição | Ajustado parêntese de fechamento da exceção |
| Duplicação de extração de override entre runner/worker/web | Lógica descentralizada com risco de drift de precedência e validação | Centralizado em `apply_script_model_override` e `extract_script_model` |

## Verificação final

- `uv run ruff check src tests` → `All checks passed!`
- `uv run pytest tests/test_agent_catalog.py tests/test_api_v2.py tests/test_web_endpoints.py tests/test_web_prompts.py tests/test_web_spa.py tests/test_runner_service.py tests/test_runtime_contract.py tests/test_runtime_mode.py tests/test_creative_contracts.py tests/test_creative_plan_graph.py tests/test_creative_agent_tools.py --no-cov` → `260 passed`

## Pendências ou bloqueios externos

- Nenhum. Modelo DeepSeek está integrado, validado e disponível para seleção per-campanha com proteção fail-closed.
