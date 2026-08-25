# Consolidação de primitivas duplicadas em `orchestrator/common`

Data: `2026-08-25`

## Resultado

Criado o pacote `src/orchestrator/common/` como fonte única de quatro primitivas que
estavam duplicadas (e, em dois casos, divergentes) pelo código: `to_plain`
(`common/plain.py`, antes em `worker.py`, `progress.py`, `legacy_import.py` e
`web/server.py`), `wav_data_uri` (`common/media.py`, antes em `nodes/stages.py` e
`adapters/mock.py`), os status terminais de prediction (`common/statuses.py`, antes em
`tools/video.py`, `adapters/replicate_video.py` e `db/effects.py`) e a inferência de
gênero com fallback por paridade (`common/gender.py`, antes em `adapters/base.py` e
`tools/creators.py`). Todos os sites originais agora importam do pacote com aliases
privados locais, preservando os nomes internos usados nos próprios módulos. Cobertura
nova em `tests/test_common_primitives.py` (38 testes).

## Mudanças de contrato

Nenhuma API pública de produto mudou. Detalhes internos relevantes:

- **`to_plain` unificado na variante mais completa** (a de `worker.py`):
  `model_dump(mode="json")`, tuplas convertidas em listas e chaves de dict
  stringificadas. Efeitos observáveis nos sites que tinham variantes menores:
  `progress.py`/`legacy_import.py`/`web/server.py` agora serializam datas/UUIDs dentro
  de models pydantic para JSON e normalizam chaves não-string; payloads seguem sendo
  estruturas JSON-like, o que é o contrato declarado desses pontos.
- **`wav_data_uri`: os dois sites NÃO eram byte-idênticos.** `nodes/stages.py`
  derivava o hash direto dos seed parts; `adapters/mock.py` prefixava
  `"voice-preview"` ao seed (`_digest_bytes`). O primitivo canônico recebe os seed
  parts puros (comportamento do `stages`) e o `MockAdapter` mantém um wrapper local
  que injeta o prefixo — saídas byte-exatas preservadas nos dois caminhos.
- **Statuses terminais**: os três conjuntos eram idênticos
  (`frozenset({"succeeded", "failed", "canceled"})`); unificados sem alteração de
  conteúdo. Aliases locais mantidos (`_TERMINAL_PREDICTION_STATUSES`,
  `_PROVIDER_TERMINAL_STATUSES`).
- **Gênero**: listas de tokens eram subconjuntos divergentes; unificadas no superset
  (femininos: `female, feminina, feminino, woman, women, mulher, girl, moça, garota,
  ela, her`; masculinos: `male, masculina, masculino, man, men, homem, boy, rapaz,
  moço, garoto, ele, his`). Preservadas as semânticas dos sites: match por substring
  sobre texto casefoldado, femininos testados antes dos masculinos e paridade
  `index % 2 == 0 -> female`. Prompts usados pelos testes de pipeline/roster foram
  conferidos contra o superset (nenhum colide com os novos tokens); os briefs
  determinísticos do mock continuam resolvendo para `neutral`.
- **Dead code**: `runner.run_cycles` NÃO foi removido — existe chamador vivo em
  `tests/test_run_cycles.py` (3 testes) e referência em `docs/DECISIONS.md` (D16),
  contra a premissa de zero callers. Mantido intacto conforme instrução.
- Documento canônico de decisões: `docs/DECISIONS.md` permanece fonte de decisões;
  esta página registra somente o resultado da consolidação.

## RED → GREEN

- **RED:** `tests/test_common_primitives.py` criado antes do pacote;
  `rtk proxy .venv/bin/python -m pytest tests/test_common_primitives.py --no-cov
  -p no:cacheprovider -q` falhou na coleta com
  `ModuleNotFoundError: No module named 'orchestrator.common'` — comportamento
  ausente demonstrado.
- **GREEN:** criação de `common/__init__.py`, `plain.py`, `media.py`,
  `statuses.py` e `gender.py`; a mesma suíte passou a verde (38 passed).
- **REFACTOR:** substituição das 11 implementações duplicadas por imports com alias
  local (`_plain`, `_to_plain`, `_wav_data_uri`, `_TERMINAL_PREDICTION_STATUSES`,
  `_PROVIDER_TERMINAL_STATUSES`), remoção de `_digest_bytes` órfão no `mock.py` e dos
  imports `base64`/`hashlib` que ficaram sem uso em `nodes/stages.py`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Ruff `E402` + `F811` em `legacy_import.py` apontando duas definições de `_plain`. | Primeira edição inseriu o import no meio do arquivo (antes de `_begin_import`) antes de eu adicionar também o import no topo. | Removido o import duplicado do meio do arquivo; mantido apenas o do topo. |
| Ruff `F401` em `adapters/base.py` (`GenderPreset` unused). | Importei o tipo sem usar na assinatura (o retorno de `infer_gender` já satisfaz `VoicePreset`). | Import reduzido a `infer_gender`. |
| Ruff `I001` em `stages.py`, `tools/creators.py`, `web/server.py`. | Imports novos inseridos fora da ordenação canônica do projeto. | `ruff check --fix` nos três arquivos. |
| `tests/test_legacy_import.py` e `tests/test_operations.py` com ERROR no setup. | `psycopg.OperationalError: connection refused 127.0.0.1:5432` — exigem PostgreSQL local; limitação de infraestrutura do sandbox, pré-existente. | Nenhuma (fora de escopo; ver Pendências). |

## Verificação final

- `rtk proxy .venv/bin/python -m pytest tests/test_common_primitives.py --no-cov
  -p no:cacheprovider -q` — 38 passed (RED confirmado antes do GREEN).
- Bateria dos módulos tocados (excluindo os que exigem PostgreSQL):
  `test_voice_profile, test_progress, test_run_cycles, test_small_gaps,
  test_adapters_mock, test_creator_voice_contracts, test_stages_coverage,
  test_stages_reroll, test_system_prompt, test_tools, test_paid_video_effects,
  test_paid_image_effects, test_paid_voice_effects, test_video_agent_node,
  test_web_endpoints, test_web_item_updates` + `test_common_primitives`
  — **419 passed**.
- Bateria complementar offline: `test_graph_e2e, test_creative_plan_graph,
  test_feedback_loop, test_feedback_store, test_resume_partial,
  test_runtime_contract_resume, test_registry_composite, test_replicate_voice,
  test_voice_factory, test_tracing, test_tracing_coverage, test_retention_wiring,
  test_staging_contract, test_concept_bias, test_agent_catalog, test_creator_real,
  test_ffmpeg_assembly, test_latentsync_pipeline` — **264 passed**.
- `rtk proxy .venv/bin/ruff check <todos os arquivos tocados>` — All checks passed.
- Grep de confirmação: nenhum restho de `_FEMALE_HINTS/_MALE_HINTS/_FEMALE_TOKENS/
  _MALE_TOKENS/frozenset({"succeeded"...})` fora de `common/`; `run_cycles` mantido
  (chamador em `tests/test_run_cycles.py`).

## Pendências ou bloqueios externos

- `tests/test_legacy_import.py` (6 erros de setup) e `tests/test_operations.py`
  (7 erros de setup) exigem servidor PostgreSQL em `127.0.0.1:5432`, indisponível
  neste sandbox; falha pré-existente de infraestrutura, não relacionada a esta mudança.
- Premissa da tarefa sobre zero callers de `runner.run_cycles` estava incorreta;
  se a remoção for desejada, primeiro remover `tests/test_run_cycles.py` e a decisão
  D16 de `docs/DECISIONS.md`.
