# Persistência e validação do runtime contract dos runs

Data: `2026-08-15`

## Resultado

Implementação do runtime contract persistido e validado para pipelines e runs:
- Todo novo run calcula e persiste um `runtime_contract` no estado inicial contendo `graph_version`, `schema_version`, `config_hash`, `provider_aliases`, `model_ids`, `prompt_versions`, `prompt_hashes` e `fingerprint` canônico.
- Segredos, tokens e credenciais são recursivamente removidos antes do cálculo de hash, e corpos de prompt nunca são incluídos.
- Retomadas (`resume_pipeline` e `resume_run`) comparam o fingerprint atual com o snapshot persistido antes de instanciar adapters ou realizar chamadas pagas; mismatches bloqueiam a execução com `RuntimeContractMismatchError`.
- Runs legados (sem fingerprint) são permitidos em modo mock, mas bloqueados em modo pago via `LegacyPaidResumeBlockedError`.
- Consultas de runs legados e finalizados permanecem intactas no read model.

## Mudanças de contrato

- **Módulo `orchestrator.runtime_contract`**: criado com `RuntimeContract`, `build_runtime_contract`, `validate_runtime_contract`, `RuntimeContractError`, `RuntimeContractMismatchError` e `LegacyPaidResumeBlockedError`.
- **Estado do grafo (`BatchState`)**: adicionada a chave opcional `runtime_contract: dict[str, Any]`.
- **Runner (`runner.py`)**: `run_pipeline` injeta `runtime_contract` no `init` do grafo; `resume_pipeline` valida o contrato contra o checkpoint antes de instanciar dependências externas.
- **Worker (`worker.py`)**: `RuntimeContractError` é tratado como falha não-repetível (`retryable=False`).

## RED → GREEN

- **RED:** `pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py` falhou com `ModuleNotFoundError: No module named 'orchestrator.runtime_contract'` e ausência de validação de resume com fingerprint mismatch / legacy paid run.
- **GREEN:** criação do módulo `src/orchestrator/runtime_contract.py`, inclusão do campo em `BatchState`, injeção em `runner.run_pipeline` e validação prévia em `runner.resume_pipeline`.
- **REFACTOR:** ordenação e sanitização determinística de configurações e credenciais, e antecipação da validação antes da instanciação de dependências pesadas (`_build_config`).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `KeyError: 'adapter'` ao rodar testes sintéticos legados | Setup de teste invocava o grafo diretamente com dict ao invés de `ToolContext`/config construído por `_build_config`. | Ajustado setup de teste para usar `runner._build_config`. |
| `RuntimeError: AI_GATEWAY_API_KEY ... is required` em teste de legacy paid resume | `_build_config` instanciava dependências pagas antes da validação do contrato. | Antecipada a validação do `runtime_contract` em `resume_pipeline` para ocorrer antes de `_build_config`. |

## Verificação final

- `rtk proxy .venv/bin/pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py --no-cov -q`: 13 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_language_runtime.py tests/test_approval_gate.py tests/test_stage_executor.py tests/test_builder.py tests/test_checkpoint.py tests/test_cli.py tests/test_graph_e2e.py --no-cov -q`: 72 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_progress_docs.py --no-cov -q`: 5 testes passando (100%).

## Pendências ou bloqueios externos

Nenhum.
