# Contratos de prompt dos agents LangChain

Data: `2026-08-14`

## Resultado

O runtime LangChain agora separa controles server-owned de dados de campanha não
confiáveis, mantém ToolStrategy como terminal estruturado e valida os invariantes de
creators, hook inicial e evidência de performance. Os prompts dos três perfis foram
reduzidos a contratos operacionais v3 idênticos.

## Mudanças de contrato

- Mensagens agentic têm `SERVER_EXECUTION_CONSTRAINTS` separado de
  `UNTRUSTED_STAGE_DATA`; somente a allowlist por stage pode entrar no primeiro.
- `creator_profiles` usa `CreatorRosterSubmission` canônico (exatamente dois creators,
  índices 0/1); output agentic de scripts exige spoken beat inicial `hook`.
- Materialização rejeita `evidence_basis="performance"` sem snapshot de performance.
- Prompts passam a `concepts-v3`, `scripts-v3` e `creators-v3`; schema permanece
  `creative-v2`.
- Decisão arquitetural: [`ADR-D47`](../../ADR-D47-langchain-agent-prompt-contracts.md).

## RED → GREEN

- **RED:** o novo teste de contratos falhou em sete casos: não havia envelope separado;
  creators usava modelo permissivo próprio; scripts aceitavam body inicial;
  performance sem snapshot era materializada; e os prompts ainda continham chamadas,
  retry e regras fixas obsoletas.
- **RED adicional:** um adapter/tool com `bias` e campanha sem snapshot fazia o
  fallback para `evidence_basis="performance"`; o guard de materialização rejeitava
  a saída apesar de o adapter não ter fornecido essa evidência.
- **GREEN:** o fallback agora escolhe `cold_test` sem snapshot; `provided_fact` e
  `performance` explicitamente fornecidos continuam sob validação do contrato.
- **GREEN original:** allowlist e mensagens separadas no `LanguageRuntime`, alias para
  o contrato canônico, validator apenas no output agentic, guarda de performance e
  prompts v3 mínimos fizeram o conjunto focado passar.
- **REFACTOR:** a mensagem confiável usa `SystemMessage`; limites de scripts com valor
  ausente permanecem explicitamente `null`, sem promover campos arbitrários.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Teste existente esperava `concepts-v2` e não encontrava `UNTRUSTED DATA`. | O teste e o texto refletiam o contrato anterior, enquanto a entrega exige v3 e a fronteira textual explícita. | Atualizado o aceite para v3 e incluída a marcação de dados não confiáveis no shared prompt. |
| Primeiro teste RED não iniciou com `python`. | O sandbox não expõe `python` no PATH. | Reexecutado com `.venv/bin/python`, sem alterar ambiente nem usar rede. |
| Seis testes de `test_legacy_import.py` falharam no setup da fixture. | A seleção ampliada incluiu testes PostgreSQL sem servidor em `127.0.0.1:5432`; a primeira reprodução foi `test_import_legacy_cli_applies_and_reports_noop_on_reexecution`. | Classificado como limitação de infraestrutura; testes PostgreSQL não são aceites offline. |
| Uma tentativa do agente raiz referenciou `tests/test_feedback.py`, `tests/test_feedback_store.py` e `tests/test_loop.py`. | `test_feedback.py` e `test_loop.py` não existem; `test_feedback_store.py` é válido, mas a seleção omitia `test_feedback_loop.py` e `test_run_cycles.py`. | Corrigida a seleção para manter `test_feedback_store.py` e incluir os dois testes reais de loop. |

## Verificação final

- `rtk proxy .venv/bin/python -m pytest tests/test_langchain_agent_contracts.py --no-cov -q` — passou (38).
- `rtk proxy .venv/bin/python -m pytest tests/test_langchain_agent_contracts.py tests/test_language_runtime.py tests/test_agent_prompt_security.py tests/test_creative_contracts.py tests/test_creative_agent_tools.py tests/test_stage_executor.py tests/test_tools.py tests/test_concept_bias.py tests/test_feedback_store.py tests/test_feedback_loop.py tests/test_run_cycles.py tests/test_agent_catalog.py tests/test_live_config_no_mock.py tests/test_staging_contract.py tests/test_progress_docs.py --no-cov` — 192 passaram; contratos, runtime, ferramentas, loops, catálogos, perfis e documentação ficaram verdes.
- `rtk proxy .venv/bin/python -m pytest tests/test_tools.py tests/test_concept_bias.py tests/test_feedback_loop.py tests/test_run_cycles.py --no-cov -q` — passou; regressão de bias e loops de feedback cobertos.
- `rtk proxy .venv/bin/python -m compileall -q src tests` — passou; paridade byte a byte dos quatro prompts nos três perfis — passou.
- `rtk proxy .venv/bin/python -m pytest tests/test_legacy_import.py --no-cov -q` — 10 passaram e 6 falharam no setup por ausência de PostgreSQL em `127.0.0.1:5432`.
- Suíte ampliada com seleção incompleta dos módulos que usam fixture PostgreSQL exibiu 13 erros por volta de 41%; a primeira reprodução foi em `test_legacy_import.py`, tentando conectar a `127.0.0.1:5432`. Depois foi interrompida nos testes longos de R2, sem falha funcional observada neste corte. A limitação é de seleção/infraestrutura, não autorização para afrouxar asserções.

## Pendências ou bloqueios externos

Servidor PostgreSQL local em `127.0.0.1:5432` é necessário para concluir a suíte
ampliada com os módulos de fixture PostgreSQL; nenhum teste foi afrouxado ou omitido
para mascarar essa limitação.
