# Persistência e validação do runtime contract dos runs

Data: `2026-08-15`

## Resultado

Implementação do runtime contract persistido e validado para pipelines e runs:
- Todo novo run calcula e persiste um `runtime_contract` no estado inicial contendo `graph_version`, `schema_version`, `config_hash`, `provider_aliases`, `model_ids`, `prompt_versions`, `prompt_hashes` e `fingerprint` canônico.
- Resolução dinâmica dos modelos efetivos de LLM (`model_ids["llm"]` e estágios de agentes `concepts`, `scripts`, `creator_profiles`) e imagem de creator (`model_ids["creator_image"]`), detectando trocas de variáveis de ambiente (`AI_GATEWAY_LLM_MODEL`, `AI_GATEWAY_OPENAI_MODEL`) e configurações de pipeline.
- Segredos, tokens e credenciais são recursivamente removidos antes do cálculo de hash, e corpos de prompt nunca são incluídos.
- Retomadas (`resume_pipeline` e `resume_run`) comparam o fingerprint atual com o snapshot persistido antes de instanciar adapters ou realizar chamadas pagas; mismatches bloqueiam a execução com `RuntimeContractMismatchError`.
- Runs legados (sem fingerprint) são permitidos em modo mock, mas bloqueados em modo pago via `LegacyPaidResumeBlockedError`.
- Consultas de runs legados e finalizados permanecem intactas no read model.

## Mudanças de contrato

- **Módulo `orchestrator.runtime_contract`**: criado com `RuntimeContract`, `build_runtime_contract`, `validate_runtime_contract`, `RuntimeContractError`, `RuntimeContractMismatchError` e `LegacyPaidResumeBlockedError`. Inclui resolução dos modelos efetivos de LLM e imagem de creator em `model_ids`.
- **Estado do grafo (`BatchState`)**: adicionada a chave opcional `runtime_contract: dict[str, Any]`.
- **Runner (`runner.py`)**: `run_pipeline` injeta `runtime_contract` no `init` do grafo; `resume_pipeline` valida o contrato contra o checkpoint antes de instanciar dependências externas.
- **Worker (`worker.py`)**: `RuntimeContractError` é tratado como falha não-repetível (`retryable=False`).

## RED → GREEN

- **RED:** `pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py` falhou inicialmente com ausência do módulo de runtime contract e subsequentemente com `KeyError: 'llm'` e `KeyError: 'creator_image'` ao testar a captura de modelos efetivos.
- **GREEN:** implementação de `build_runtime_contract` com `_resolve_effective_llm_model` e `_resolve_effective_creator_image_model`, persistência em `runner.run_pipeline` e validação prévia em `runner.resume_pipeline`.
- **REFACTOR:** ordenação e sanitização determinística de configurações e credenciais, e antecipação da validação antes da instanciação de dependências pesadas (`_build_config`).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `KeyError: 'adapter'` ao rodar testes sintéticos legados | Setup de teste invocava o grafo diretamente com dict ao invés de `ToolContext`/config construído por `_build_config`. | Ajustado setup de teste para usar `runner._build_config`. |
| `RuntimeError: AI_GATEWAY_API_KEY ... is required` em teste de legacy paid resume | `_build_config` instanciava dependências pagas antes da validação do contrato. | Antecipada a validação do `runtime_contract` em `resume_pipeline` para ocorrer antes de `_build_config`. |
| Troca de `AI_GATEWAY_LLM_MODEL` ou `AI_GATEWAY_OPENAI_MODEL` não alterava fingerprint quando `target_model` era `None` | `model_ids` só registrava `spec.target_model` explícito e não consultava os modelos resolvidos pelo runtime/provider. | Adicionada resolução de modelo efetivo para linguagem e imagem de creator em `build_runtime_contract`. |

## Verificação final

- `rtk proxy .venv/bin/pytest tests/test_runtime_contract.py tests/test_runtime_contract_resume.py --no-cov -q`: 17 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_language_runtime.py tests/test_approval_gate.py tests/test_stage_executor.py tests/test_builder.py tests/test_checkpoint.py tests/test_cli.py tests/test_graph_e2e.py --no-cov -q`: 72 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_progress_docs.py --no-cov -q`: 5 testes passando (100%).

## Pendências ou bloqueios externos

Nenhum.
