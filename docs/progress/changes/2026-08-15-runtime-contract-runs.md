# Persistência e validação do runtime contract dos runs

Data: `2026-08-15`

## Resultado

Implementação do runtime contract persistido e validado para pipelines e runs:
- Todo novo run calcula e persiste um `runtime_contract` no estado inicial e nos endpoints locais (`server.py`), contendo `graph_version`, `schema_version`, `config_hash`, `provider_aliases`, `model_ids`, `prompt_versions`, `prompt_hashes`, `stage_executors`, `stage_tools`, `stage_schema_versions`, `stage_agent_enabled` e `fingerprint` canônico.
- Resolução dinâmica e consistente dos modelos efetivos de LLM (`model_ids["llm"]` e estágios de agentes `concepts`, `scripts`, `creator_profiles`) e imagem de creator (`model_ids["creator_image"]`), detectando trocas de variáveis de ambiente (`AI_GATEWAY_LLM_MODEL`, `AI_GATEWAY_OPENAI_MODEL`) e configurações de pipeline.
- Segredos, tokens e credenciais são recursivamente removidos antes do cálculo de hash, e corpos de prompt nunca são incluídos.
- Retomadas (`resume_pipeline` e `resume_run`) comparam o fingerprint atual com o snapshot persistido antes de instanciar adapters ou realizar chamadas pagas; mismatches bloqueiam a execução com `RuntimeContractMismatchError`.
- Runs legados (sem fingerprint) são permitidos em modo mock, mas bloqueados em modo pago via `LegacyPaidResumeBlockedError`. Adapters de judge não são classificados falsamente como adapters de execução paga em `config-mock`.
- Consultas de runs legados e finalizados permanecem intactas no read model.

## Mudanças de contrato

- **Módulo `orchestrator.runtime_contract`**: criado com `RuntimeContract`, `build_runtime_contract`, `validate_runtime_contract`, `RuntimeContractError`, `RuntimeContractMismatchError` e `LegacyPaidResumeBlockedError`. Inclui resolução dos modelos efetivos de LLM e imagem de creator em `model_ids`, e propriedades declarativas de estágio (`stage_executors`, `stage_tools`, `stage_schema_versions`, `stage_agent_enabled`) no fingerprint.
- **Estado do grafo (`BatchState`)**: adicionada a chave opcional `runtime_contract: dict[str, Any]`.
- **Servidor Web (`server.py`)**: `_run_task` calcula e injeta `runtime_contract` em `run_state` e no payload `init` da execução local.
- **Runner (`runner.py`)**: `run_pipeline` injeta `runtime_contract` no `init` do grafo; `resume_pipeline` valida o contrato contra o checkpoint antes de instanciar dependências externas e avalia estritamente papéis de execução (`ROLES` ou `llm`) para classificar execuções pagas.
- **Worker (`worker.py`)**: `RuntimeContractError` é tratado como falha não-repetível (`retryable=False`).

## RED → GREEN

- **RED:** `pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py` falhou inicialmente com ausência do módulo de runtime contract, e posteriormente com falhas em propriedades de catálogo e modelos de imagem.
- **GREEN:** implementação completa de `build_runtime_contract`, persistência no runner e web server, validação prévia no resume e cobertura em unit/integration tests.
- **REFACTOR:** ordenação e sanitização determinística de configurações e credenciais, e antecipação da validação antes da instanciação de dependências pesadas (`_build_config`).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `KeyError: 'adapter'` ao rodar testes sintéticos legados | Setup de teste invocava o grafo diretamente com dict ao invés de `ToolContext`/config construído por `_build_config`. | Ajustado setup de teste para usar `runner._build_config`. |
| `RuntimeError: AI_GATEWAY_API_KEY ... is required` em teste de legacy paid resume | `_build_config` instanciava dependências pagas antes da validação do contrato. | Antecipada a validação do `runtime_contract` em `resume_pipeline` para ocorrer antes de `_build_config`. |
| Troca de `AI_GATEWAY_LLM_MODEL` ou `AI_GATEWAY_OPENAI_MODEL` não alterava fingerprint quando `target_model` era `None` | `model_ids` só registrava `spec.target_model` explícito e não consultava os modelos resolvidos pelo runtime/provider. | Adicionada resolução de modelo efetivo para linguagem e imagem de creator em `build_runtime_contract`. |
| `config-mock` bloqueado em legacy resume por presença de `judge: gateway` | Classificação de `is_paid` verificava o papel `judge` além dos papéis de execução. | Restringida a checagem de execução paga para `role in ROLES or role == "llm"`. |

## Verificação final

- `rtk proxy .venv/bin/ruff check .`: 0 erros (100% limpo).
- `rtk proxy .venv/bin/pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py --no-cov -q`: 19 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_web_endpoints.py tests/test_progress_docs.py --no-cov -q`: 89 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_language_runtime.py tests/test_approval_gate.py tests/test_stage_executor.py --no-cov -q`: 26 testes passando (100%).

## Pendências ou bloqueios externos

Nenhum.
