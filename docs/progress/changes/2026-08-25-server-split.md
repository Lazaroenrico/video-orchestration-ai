# Split de `web/server.py` em composition root + módulos por domínio

Data: `2026-08-25`

## Resultado

`src/orchestrator/web/server.py` caiu de 2.253 para 625 linhas e virou composition
root (app FastAPI, lifespan/runner embarcado, webhook Replicate, handlers 503,
healthz/readyz, montagens estáticas de mídia/assets no mesmo momento de import,
inclusão dos routers e re-exports de retrocompatibilidade). O restante foi extraído,
sem mudança de comportamento, para:

| Módulo novo | Conteúdo | Linhas |
| --- | --- | --- |
| `web/events.py` | normalização de eventos/projeções puras (`_persisted_event_payload`, `_normalize_*`, `_snapshot_from_item`, `_build_item_update`, `_runtime_phase`, `_safe_serialize`, `DATABASE_UNAVAILABLE_ERRORS`, etc.) | 351 |
| `web/run_executor.py` | `_execute_run`, `_emit`/`_emit_sync`, sentinela `_RUN_REPOSITORY_UNSET`, helper de lookup tardio `_server_attr` | 407 |
| `web/runs_registry.py` | `RunRegistry` (ex-dict global `_runs`) + singleton `REGISTRY` + `pending_creators_for` | 72 |
| `web/routes_runs.py` | start V1/V2, retry, stream SSE, list/status/state + models de request | 663 |
| `web/routes_review.py` | gate humano V2 (review/approve/concepts/reroll-voice) + models | 322 |
| `web/routes_content.py` | prompts, creators history, integrations + recuperação de mídia | 179 |
| `web/settings.py` | defaults de config dir e leitura de env compartilhados | 20 |

O par `_signing_storage`/`_sign_payload` permaneceu no composition root de propósito:
testes fazem patch das dependências deles (`load_providers`, `build_media_storage`,
`_media_root`) via namespace do `server`, e movê-los exigiria shims extras sem ganho.

## Mudanças de contrato

Nenhuma. Refactor estritamente mecânico e behavior-preserving:

- Gate humano **dual-mode preservado intacto** — modo local continua resolvendo via
  Future no estado do registro (`routes_review.review_run_v2` faz `set_result`;
  `run_executor._execute_run` cria/aguarda o Future no interrupt
  `review_creative_plan`); modo durável continua nos gates de PostgreSQL com 409
  stale / 410 cancelado, corpo verbatim em `routes_review`. Os dois modos NÃO foram
  unificados.
- `_runs`: semântica observável inalterada — segue puramente in-memory (sem
  persistência), mesma instância exposta como `server._runs` (protocolo de dict
  preservado: `get/setitem/getitem/pop/clear/items/in/len`) e agora também em
  `app.state.runs`. Criação passou a usar `RunRegistry.create(run_id)` com o mesmo
  shape `{"queues": [], "buffer": [], "done": False}`.
- Todos os símbolos que testes importam de `orchestrator.web.server` continuam
  acessíveis (re-exports declarados em `__all__`), incluindo módulos expostos como
  atributos (`job_store`, `run_store`, `prompt_store`, `creator_store`, `runner`,
  `stream_bus`). Zero arquivos de teste existentes precisaram ser editados.
- Colaboradores que os testes fazem `monkeypatch.setattr(web_server, ...)`
  (`load_pipeline/providers/agent_catalog`, `open_checkpointer`, `build_graph`,
  `run_trace_config`, `default_creator_store_path`, `default_media_path`, `_emit`,
  `_emit_sync`, `_find_creator_for_draft_repository`, `_wake_web_embedded_runner`,
  `_sign_payload`, `_signing_storage`) são resolvidos tardiamente via
  `run_executor._server_attr` no momento da chamada — sem ciclo de import.

## RED → GREEN

Refactor guiado pela suíte existente como contrato (TDD de preservação):

- **Baseline:** `tests/test_web_endpoints.py` → 84 passed antes de qualquer edição.
- **Estágios** (suíte rodada após cada um, sempre verde):
  1. `events.py` + `settings.py` (helpers puros fora do server);
  2. `runs_registry.py` substituindo o dict global `_runs` (21 referências internas
     passam pelo registry);
  3. `run_executor.py` (`_execute_run` + emissão SSE + plumbing de Future/token);
  4. módulos de rotas com `APIRouter` incluídos na ordem relativa original
     (`spa_fallback` permanece registrado por último) + re-exports;
  5. limpeza do composition root (ruff zero).
- **RED→GREEN (novo):** `tests/test_web_runs_registry.py` (4 testes) cobre o
  contrato do `RunRegistry` (shape canônico de `create`, protocolo de dict, aliases
  `server._runs`/`app.state.runs` apontando para o singleton, validações 404/409 de
  `pending_creators_for`).
- **REFACTOR:** n/a além do próprio split.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `test_local_start_signs_seed_and_can_persist_run_index` falhou: seed veio do lookup real em vez do fake. | `start_run` (agora em `routes_runs`) importava `_find_creator_for_draft_repository` estaticamente; o `monkeypatch.setattr(web_server, "_find_creator_for_draft_repository", ...)` ficou invisível. | Resolver via `_server_attr("_find_creator_for_draft_repository")` no momento da chamada. |
| 30 falhas após `ruff --fix`: `AttributeError: module 'orchestrator.web.server' has no attribute 'prompt_store'/'job_store'/...` | O autofix removeu imports de módulo e colaboradores que viraram "não usados" dentro do server, mas que são superfície de patch/atributo dos testes (`web_server.job_store`, `setattr(web_server, "run_trace_config", ...)` etc.). | Restaurar esses nomes como re-exports explícitos listados em `__all__` e validar com script ad hoc que confere `hasattr(server, <todo atributo referenciado como web_server.X/server.X nos testes>)` → "all referenced server attributes exist". |

## Verificação final

- `rtk proxy python -m pytest tests/test_web_endpoints.py --no-cov -p no:cacheprovider -q` → 84 passed (após cada estágio e ao final).
- `rtk proxy python -m pytest tests/test_web_endpoints.py tests/test_web_runs_registry.py ... -q` → 88 passed.
- Demais suítes que importam símbolos de `web.server` (rodadas integralmente, todas verdes):
  `test_api_v2.py`, `test_web_prompts.py`, `test_web_spa.py`, `test_server_signed_urls.py`,
  `test_adapters_mock.py`, `test_replicate_webhook.py`, `test_graph_topology.py`,
  `test_web_item_updates.py`, `test_agent_catalog.py`, `test_paid_video_effects.py`.
- Suítes PostgreSQL (`test_postgres_jobs/runs/prompts/creators`): coleta OK (ex. 19
  testes em `test_postgres_runs.py`), execução exige servidor em `127.0.0.1:5432`
  (limitação conhecida de infra local; referências delas a `web_server.*` cobertas
  pelo checker de atributos).
- `ruff check src/orchestrator/web/*.py tests/test_web_runs_registry.py` → All checks passed.
- Script `/tmp/check_server_attrs.py` (ad hoc, não versionado) → todos os atributos
  `web_server.X`/`server.X` referenciados em `tests/test_*.py` existem.

## Pendências ou bloqueios externos

- Split de `tests/test_web_endpoints.py` (2.110 linhas) adiado para um próximo
  round — prioridade era manter os módulos verdes; o arquivo permanece intacto.
- Suítes PostgreSQL seguem dependendo de servidor local (infra, não código).
