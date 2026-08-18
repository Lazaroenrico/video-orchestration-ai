# Isolamento do GatewayJudge no módulo de evaluation

Data: `2026-08-15`

## Resultado

O `GatewayJudge` e todo o ferramental de avaliação determinística (LLM-as-judge via cassettes e evaluators) foram migrados para o módulo dedicado `src/orchestrator/evaluation/` (`orchestrator.evaluation.judge`).
O protocolo `JudgePort` foi removido de `src/orchestrator/adapters/base.py`, `JudgeVerdict` foi removido de `src/orchestrator/graph/state.py` e o adapter `judge: gateway` foi retirado de `config*/providers.yaml`.
O endpoint `/readyz` do servidor web não mais executa validação de `load_judge`, assegurando que o runtime de produção seja completamente desacoplado de ferramentas de avaliação.

## Mudanças de contrato

- `GatewayJudge`, `Cassette`, `CassetteMiss`, `JudgeVerdict`, `DEFAULT_QC_CRITERIA`, `SCOPE_CRITERIA`, `qc_correctness_evaluator`, `scope_adherence_evaluator`, `evaluate_judge` e `dig` agora são importados de `orchestrator.evaluation` (ou `orchestrator.evaluation.judge`).
- `JudgePort` foi removido de `orchestrator.adapters.base`.
- `JudgeVerdict` foi removido de `orchestrator.graph.state`.
- `adapters/judge.py` foi deletado.
- `judge: gateway` foi removido de `config/providers.yaml`, `config-mock/providers.yaml` e `config-staging/providers.yaml`.
- Decisão canônica registrada em [D47](../../DECISIONS.md#d47--isolamento-do-gatewayjudge-no-módulo-orchestratorevaluation).

## RED → GREEN

- **RED:** `test_judge_eval.py` e `test_scope_eval.py` falharam na importação com `ModuleNotFoundError: No module named 'orchestrator.evaluation'`.
- **GREEN:** Criado pacote `src/orchestrator/evaluation/` contendo `__init__.py` e `judge.py` com as implementações de `GatewayJudge`, `Cassette`, `CassetteMiss`, `JudgeVerdict`, evaluators e helpers.
- **RED:** `test_state.py::test_judge_verdict_not_in_graph_state` falhou com `AssertionError` pois `JudgeVerdict` ainda residia em `orchestrator.graph.state`.
- **GREEN:** Removida a definição de `JudgeVerdict` de `src/orchestrator/graph/state.py` e os testes de `JudgeVerdict` foram movidos para `tests/test_judge_eval.py`.
- **RED:** `test_registry_composite.py::test_judge_port_and_role_not_in_production_adapters` falhou pois `JudgePort` existia em `orchestrator.adapters.base`.
- **GREEN:** Removida `class JudgePort(Protocol)` e o import de `JudgeVerdict` de `src/orchestrator/adapters/base.py`.
- **RED:** `test_registry_composite.py::test_providers_yaml_does_not_contain_judge_adapter` falhou pois `judge: gateway` estava presente nos arquivos `providers.yaml`.
- **GREEN:** Removida a linha `judge: gateway` de `config/providers.yaml`, `config-mock/providers.yaml` e `config-staging/providers.yaml`.
- **RED:** `test_web_spa.py` e `web/server.py` mantinham acoplamento a `load_judge` no handler `/readyz`.
- **GREEN:** Removido `load_judge` dos imports e da função `readyz` de `src/orchestrator/web/server.py`, e removidos os monkeypatches obsoletos de `tests/test_web_spa.py`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'orchestrator.evaluation'` ao rodar testes de avaliação | Módulo `src/orchestrator/evaluation/` ainda não existia | Criado módulo `src/orchestrator/evaluation/` exportando `GatewayJudge`, `Cassette`, `CassetteMiss`, `JudgeVerdict`, evaluators e helpers |
| `JudgeVerdict` ainda importável de `graph.state` | Classe pertencia ao modelo de estado geral da pipeline | Removida de `state.py` e mantida exclusivamente em `orchestrator.evaluation.judge` |
| `JudgePort` presente nos protocolos de adapters de produção | `JudgePort` estava declarado em `adapters/base.py` | Removido `JudgePort` de `adapters/base.py` e confirmado que `ROLES` contém apenas papéis de mídia/domínio |
| Chave `judge` em `providers.yaml` | Declaração informativa legada nos perfis de configuração | Removida linha `judge: gateway` de `config/providers.yaml`, `config-mock/providers.yaml` e `config-staging/providers.yaml` |
| `readyz` validando `load_judge` | Servidor web checava carga de `judge.yaml` na verificação de readiness do runtime | Removida chamada a `load_judge` de `readyz` em `src/orchestrator/web/server.py` |

## Verificação final

- `PYTHONPATH=. rtk proxy uv run pytest --no-cov tests/test_judge_eval.py tests/test_scope_eval.py tests/test_web_spa.py tests/test_live_config_no_mock.py tests/test_registry_composite.py tests/test_api_v2.py`: 91 passed, 2 skipped (100% de sucesso).
- `PYTHONPATH=. rtk proxy uv run pytest --no-cov tests/test_progress_docs.py`: 4 passed (validação estrita de documentação, links, âncoras e integridade dos manifestos).
- Cassettes `tests/cassettes/judge_qc.json` e `tests/cassettes/scope_eval.json` preservados sem nenhuma regravação não intencional.

## Pendências ou bloqueios externos

Nenhum.
