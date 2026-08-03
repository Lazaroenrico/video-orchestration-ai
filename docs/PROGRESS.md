# PROGRESS — handoff

## Cache persistente do front com TanStack Query (2026-07-28)

Objetivo: reduzir waterfalls repetidos no dashboard React (`getRuns` + vários
`getStatus`) e manter resultados anteriores disponíveis após navegação/reload.

### Red → Green e falhas investigadas

- **RED:** `front/src/api/queries.contract.ts` falhou no `tsc` com
  `Cannot find module './queries'`. **Causa:** não existia uma camada pública de
  cache/query keys para o front. **Correção:** criada a camada TanStack Query com
  query keys estáveis, hooks cacheados, agregação de campanhas e mutations que
  invalidam runs/gates/prompts/creators.
- **Persistência:** o app agora usa `PersistQueryClientProvider` com `localStorage`,
  `maxAge` de 12h, `gcTime` de 24h e `buster` versionado. Somente queries
  bem-sucedidas e com payload serializado até 500 KB entram no cache persistente.
- **SSE:** `useRunStream` hidrata o snapshot inicial via QueryClient e atualiza ou
  invalida cache quando recebe `run_end`, gates, item updates, erro e creator updates.
  Tokens/logs de LLM continuam apenas em memória.

**Verificação local:** `rtk npm run typecheck`, `rtk npm run check:boundaries` e
`rtk npm run build` verdes em `front/`.

## Runner embutido no web para campanhas duráveis locais (2026-07-28)

Objetivo: permitir que a criação/aprovação de campanha pelo dashboard avance no caminho
durável sem exigir um processo `orchestrator runner` separado durante desenvolvimento.

### Red → Green e falhas investigadas

- **RED:** `test_start_run_defaults_to_staging_config_and_wakes_runner` falhou porque
  `POST /api/run` persistia o job com `config_dir=None` e não sinalizava nenhum executor.
  **Causa:** com `DATABASE_URL`, o endpoint só enfileira `execute_run`; sem runner ativo,
  o job fica `queued`, nenhum gate é aberto em `run_gates` e o front observa a campanha
  parada. **Correção:** o web agora usa `config-staging` como default durável e, quando
  `ORCH_WEB_EMBEDDED_RUNNER=true`, inicia um loop interno que chama `run_worker_once()`.
- **Gates e retry:** aprovações de creator/conceitos e retry manual agora acordam o
  runner embutido depois de `resolve_gate()`/`enqueue_run()`. Isso cobre o ciclo front
  → gate persistido → job de resume sem depender de polling longo.
- **Escopo:** o runner embutido é opt-in e só liga com `DATABASE_URL`; produção/runner
  dedicado continuam no contrato existente. O loop captura falhas de job em log para não
  derrubar a API e é cancelado no lifespan shutdown.

**Verificação local:** `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_web_endpoints.py`
→ **56 passed**; `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_web_spa.py tests/test_web_prompts.py tests/test_web_item_updates.py tests/test_runner_service.py tests/test_sqs_runner.py`
→ **68 passed**. A suíte PostgreSQL relevante foi tentada, mas o fixture falhou antes do
código por ausência/configuração local de PostgreSQL em `127.0.0.1:5432`
(`fe_sendauth: no password supplied`), limitação já prevista nas instruções do projeto.

## Cancelamento em conexão PostgreSQL encerra transação antes de descarte (2026-07-28)

Objetivo: reduzir warnings do psycopg em SSE/requests cancelados, como
`another command is already in progress` e `Explicit rollback() forbidden within a
Transaction context` durante cleanup de conexão async ativa, e aceitar probes
`HEAD /healthz` sem ruído `405`.

### Red → Green e falhas investigadas

- **RED:** `test_database_connection_closes_connection_on_cancellation_before_rollback`
  falhou porque `Database.connection()` deixava o `async with connection.transaction()`
  executar `__aexit__`/rollback quando o corpo era cancelado. **Causa:** em async Python,
  uma exceção lançada no `yield` de um `@asynccontextmanager` sai primeiro pelos context
  managers internos; portanto o rollback automático da transação roda antes de qualquer
  `except` externo conseguir fechar a conexão. Isso reproduz a classe do warning visto em
  produção: uma query ainda ativa impede o rollback e o pool descarta a conexão como `BAD`.
- **RED adicional:** `test_database_connection_exits_transaction_before_closing_on_cancellation`
  falhou porque a correção anterior fechava a conexão cancelada sem chamar
  `transaction.__aexit__()`. **Causa:** o contexto `Transaction` do psycopg ficava ativo;
  ao receber a conexão de volta, o pool tentava `connection.rollback()` explicitamente e o
  psycopg recusava rollback explícito dentro de um `Transaction` aberto. **Correção:**
  `Database.connection()` agora chama `transaction.__aexit__(CancelledError, exc, tb)` em
  `asyncio.shield()` antes de fechar/descartar a conexão cancelada.
- **GREEN:** fluxo normal segue chamando `transaction.__aexit__()` para commit/rollback
  como antes. Em `asyncio.CancelledError`, a transação é encerrada formalmente e a conexão
  ainda é fechada com `asyncio.shield(connection.close())`. **Trade-off aceito:** em
  cancelamentos frequentes o pool recria mais conexões, mas evita devolver ao pool uma
  conexão em estado incerto ou com contexto de transação pendurado.
- **Health:** `test_healthz_accepts_head_for_liveness_probes` falhou porque o FastAPI
  registrava só `GET /healthz`; probes com `HEAD` caíam em `405 Method Not Allowed`.
  **Correção:** `/healthz` agora registra `HEAD` no mesmo handler de liveness sem IO.

**Verificação local:** `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_postgres_foundation.py -k 'closes_connection_on_cancellation_before_rollback or tenant_identity'`
→ **3 passed** antes da regressão observada; `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_postgres_foundation.py -k 'exits_transaction_before_closing_on_cancellation or tenant_identity'`
→ **3 passed**; `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_web_spa.py -k 'healthz_accepts_head or healthz_is_ok'`
→ **2 passed**.

## Retry manual de campanha falhada cria fork limpo (2026-07-28)

Objetivo: permitir que o dashboard tente novamente uma campanha em `error` criando uma
nova campanha, sem reusar o `run_id` antigo nem copiar estado parcial.

### Red → Green e falhas investigadas

- **RED:** `test_retry_failed_persisted_run_creates_clean_fork` falhou com
  `AttributeError: module 'orchestrator.web.server' has no attribute 'retry_run'`.
  **Causa:** não existia rota/coroutine pública para retry manual. **Correção:**
  adicionado `POST /api/run/{run_id}/retry`, que valida `phase == "error"`, lê o payload
  original do job `execute_run`, cria novo `web-...`, adiciona `source_run_id` no payload
  e enfileira uma execução limpa via `enqueue_run()`.
- **Contratos de erro:** adicionados testes para run inexistente (`404`), run não falhado
  (`409`) e run falhado sem payload inicial (`409`). **Causa coberta:** retry não deve
  funcionar como resume nem tentar reconstruir campanha a partir de estado parcial.
  **Correção:** a rota só usa o payload inicial persistido, e falha antes de enfileirar
  quando ele não existe ou não possui campos mínimos de execução.
- **Persistência:** `PostgresJobRepository.get_initial_run_payload()` recupera o primeiro
  payload `execute_run` do run; a cobertura PostgreSQL foi adicionada em
  `test_initial_run_payload_is_retrievable_for_manual_retry`.
- **Front:** `RetryRunResponse` e `api.retryRun()` foram adicionados. `CampaignDetail`
  mostra “Retry campaign” em `phase === "error"`, exibe loading/erro e navega para a
  nova campanha retornada pelo backend.
- **Docs:** `AGENTS.md` agora registra que retry manual de campanha falhada sempre cria
  novo `run_id` e nunca reutiliza o antigo.

**Verificação local:** `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_web_endpoints.py`
→ **52 passed**; `rtk npm run typecheck` e `rtk npm run check:boundaries` verdes em
`front/`. O teste PostgreSQL novo foi tentado isoladamente, mas o fixture falhou antes do
código por ausência de PostgreSQL em `127.0.0.1:5432`
(`psycopg.OperationalError: connection is bad`), a mesma limitação de infraestrutura já
documentada para este sandbox.

### Follow-up frontend

- **Sintoma:** o botão “Retry campaign” não aparecia no front servido pelo FastAPI.
  **Causa:** `front/dist` já existia e ainda continha o bundle antigo; `dashboard()` serve
  `front/dist/index.html` quando ele existe, então mudanças em `front/src` não aparecem no
  backend sem rebuild. **Correção:** `rtk npm run build` em `front/` regenerou
  `dist/assets/index-CI1DOQNM.js`; o bundle novo contém “Retry campaign” e
  `/api/run/${run_id}/retry`.
- **Segundo sintoma:** mesmo com o bundle novo, a ação só existia dentro de
  `CampaignDetail`; em Campaigns, Dashboard, Queue e Video Review não havia nenhum botão
  visível no contexto onde o usuário vê a falha. **Causa:** a implementação inicial
  seguiu a rota planejada do detalhe, mas o fluxo real do dashboard expõe campanhas
  falhadas em várias telas. **Correção:** criado `RetryCampaignButton` reutilizável e
  conectado em Campaigns (desktop/mobile), Dashboard (um erro = retry direto, múltiplos =
  ir para Campaigns), Queue e Video Review. O build servido foi regenerado para
  `dist/assets/index-DENXVYSo.js`.

## Gates duráveis: contrato front ↔ backend corrigido (2026-07-28)

Objetivo: corrigir o contrato dos human gates no dashboard quando `DATABASE_URL` está
ativo. O backend já exigia `gate_id`/`version` para resolver gates persistidos, mas o
front só enviava `approved`/`concepts`.

### Red → Green e falhas investigadas

- **RED:** `test_run_state_returns_versioned_persisted_concept_gate` e
  `test_run_state_returns_versioned_persisted_creator_gate` falharam porque
  `/api/state/{run_id}` retornava `edit_concepts=[]`/`awaiting=[]` e não retornava
  `gate` quando o run vinha do read model PostgreSQL. **Causa:** no caminho durável, o
  payload canônico do gate vive em `run_gates`, mas `/api/state` lia apenas
  `persisted.state["pending_*"]` — campo produzido pelo caminho local `_runs`, não pelo
  Runner durável. **Correção:** `/api/state` agora busca `get_pending_gate(run_id)`,
  deriva `edit_concepts`/`awaiting` do payload do gate e retorna
  `{gate_id, version, gate_type}`.
- **Contrato SSE:** `PostgresJobRepository.open_gate()` agora inclui
  `gate_id`/`version`/`gate_type` nos eventos públicos `awaiting_concept_edit` e
  `awaiting_approval`, preservando o replay por `Last-Event-ID`.
- **Front:** `RunDetail`/`StreamEvent` ganharam `GateRef`; `useRunStream` hidrata e
  reduz o gate; `CampaignDetail` e `Concepts` reenviam `gate_id`/`version` em
  aprovações/edições. O modo local continua compatível com `gate: null`.
- **Docs:** `AGENTS.md` foi atualizado para refletir `config-mock`, `config-staging`,
  `config`, `AsyncPostgresSaver`, jobs/gates/eventos duráveis e a regra de nunca resolver
  gate persistido só por `run_id`.

**Verificação local:** `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_web_endpoints.py`
→ **48 passed**; `rtk npm run typecheck`, `rtk npm run check:boundaries` e
`rtk npm run check:video-bridge` verdes. O teste PostgreSQL alterado foi coletado com
sucesso, mas não executado neste sandbox porque não há PostgreSQL em `127.0.0.1:5432`.

## D36 — Plano Cloudflare com portabilidade AWS (2026-07-16)

Foi documentado o plano em `docs/ADR-D36-cloudflare-aws-portability.md`; **nenhuma
infraestrutura ou codigo de producao foi alterado**. A ordem obrigatoria antes de qualquer
deploy e: imagem OCI API/Runner -> PostgreSQL e `AsyncPostgresSaver` -> jobs/gates/eventos
duraveis -> Worker/Containers/Queues Cloudflare -> operacao -> exercicio ECS/SQS/S3.

O ponto que nao pode ser pulado e substituir `_runs`, `BackgroundTasks`, `Future` e o buffer
SSE em memoria por fonte de verdade PostgreSQL. R2 ja esta portavel por S3; o compute so fica
portavel de verdade depois que essa persistencia existir. A D30 continua correta ao dizer que
hospedagem nao fazia parte do seu escopo; D36 abre esse escopo como uma decisao nova.

## D36 — Fase 1: empacotar como imagem OCI portável (2026-07-17)

Empacotamento **sem mudar comportamento** (o app continua em SQLite/JSON; PostgreSQL é Fase
2). Entregue:

- **Comandos do container** em `cli.py`: `api` (= `serve`, que virou alias), `runner`
  (reusa o caminho de `run` via novo helper `_do_run`; one-shot, sem fila ainda) e `migrate`
  (idempotente: materializa o schema do checkpointer + `ArtifactDB` e os dirs de mídia).
- **Health** em `web/server.py`: `GET /healthz` (liveness, sem IO) e `GET /readyz`
  (readiness: valida `load_pipeline/providers/judge` e resolve o backend de storage —
  `R2MediaStorage.from_env()` valida credencial sem request de rede; 503 com motivo se
  quebrar). Rotas explícitas vencem o catch-all SPA por ordem de registro.
- **Mounts condicionais**: `/media` e `/videos` extraídos para `_install_media_mounts`,
  guardados por `ORCH_SERVE_LOCAL_MEDIA` (default ligado; em prod R2 serve por URL assinada).
- **`R2_ENDPOINT_URL`** opcional em `r2.py:from_env` — mesmo código serve R2, MinIO (dev) e
  S3 (AWS), só trocando endpoint/credencial.
- **Infra**: `Dockerfile` multi-stage (build da SPA em Node → runtime Python 3.12 + Node LTS
  copiado da imagem oficial, para o bridge Seedance), `.dockerignore`, `docker-compose.yml`
  (app + MinIO + PostgreSQL-scaffolding), e envs novas no `.env.example`.

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **900 passed, 2 skipped**,
cobertura 100%. Build da imagem e `docker compose up` não rodados neste ambiente (sem Docker);
a verificação de container fica para quem tiver o daemon (ver plano em
`~/.claude/plans/tender-imagining-peacock.md`).

## D36 — Fase 2/T1: fundação PostgreSQL, Alembic e tenancy (2026-07-18)

Primeira fatia da Fase 2 entregue sem ligar ainda os repositórios de negócio:

- `orchestrator.db.Database` encapsula `AsyncConnectionPool`, abertura/fechamento
  explícitos e transações que aplicam `app.organization_id`/`app.user_id` com
  `set_config(..., true)`. O escopo é `SET LOCAL`, portanto não vaza quando a conexão
  volta ao pool. O boot rejeita papéis `SUPERUSER`/`BYPASSRLS` para impedir configuração
  que torne as policies inócuas.
- `TenantIdentity.from_env()` exige `ORCH_ORGANIZATION_SLUG`,
  `ORCH_ORGANIZATION_NAME` e `ORCH_USER_SUBJECT`; o bootstrap gera ids estáveis e cria
  organization, user e membership de forma idempotente.
- Alembic passou a versionar o schema PostgreSQL. A revisão `20260718_0001` cria
  `organizations`, `users` e `organization_members`, habilita e força RLS e restringe
  organizações/memberships ao tenant atual e identidades aos memberships visíveis.
- `orchestrator migrate --database-url ...` (ou `DATABASE_URL`) aplica `head`
  idempotentemente. Sem URL, o caminho SQLite da Fase 1 permanece inalterado para o modo
  mock/local.
- Dependências runtime: `alembic` e `psycopg[binary,pool]`; integração usa
  `pytest-postgresql` contra PostgreSQL 16 real, sem mock de banco.
- O Compose cria `orchestrator` como papel `NOSUPERUSER NOBYPASSRLS`; o `POSTGRES_USER`
  administrativo fica limitado ao bootstrap do volume.

### Red → Green e falhas investigadas

- RED inicial: `tests/test_postgres_foundation.py` falhou com `ModuleNotFoundError` para
  `orchestrator.db`. GREEN: migração, pool e bootstrap idempotente implementados pela
  nova interface pública.
- Sintoma: o teste RLS mostrou Alice/Acme lendo `oidc|bob` de Globex. Causa: `users` era
  global e não tinha policy. Correção: `FORCE ROW LEVEL SECURITY` em `users`, leitura por
  membership ou pela própria identidade da sessão e policies separadas de insert/update/
  delete.
- Sintoma: após a primeira policy de `users`, o bootstrap falhou com
  `InsufficientPrivilege` antes de criar o membership. Causa: `INSERT ... ON CONFLICT DO
  NOTHING` precisa avaliar a visibilidade do próprio usuário, mas a policy inicial só
  reconhecia memberships já existentes. Correção: permitir `id = app.user_id` na leitura
  e manter conflito como `DO NOTHING`, sem update cross-tenant.
- Sintoma: o Compose entregava ao app o próprio `POSTGRES_USER`, que é superuser e ignora
  RLS. Causa: o scaffolding da Fase 1 não separava bootstrap e runtime. Correção: init SQL
  cria o papel `orchestrator` sem bypass, transfere database/schema e o pool falha cedo se
  receber credenciais privilegiadas.
- Sintoma: cobertura focada ficou em 97,62% apesar dos 9 testes funcionais verdes. Causa:
  os ramos de DSN `postgresql+psycopg://` e rejeição de backend não PostgreSQL não tinham
  regressão. Correção: testes públicos para ambos; pacote `orchestrator.db` voltou a 100%.
- Sintoma: o primeiro gate global teve 3 falhas em `test_replicate_throttle` porque warnings
  não chegavam ao `caplog` depois dos testes PostgreSQL. Causa: o `fileConfig()` padrão do
  Alembic reconfigurava o root logger e desabilitava loggers existentes no processo da API.
  Correção: migração programática marca `configure_logger=False`; a CLI mantém a configuração
  da aplicação, e uma regressão prova que loggers continuam ativos após `upgrade_database()`.
- Sintoma ambiental: PostgreSQL e testes com `asyncio.to_thread` não conseguem abrir
  sockets/encerrar executor dentro do sandbox. Causa confirmada pelos logs (`Operation not
  permitted`) e stack em `asyncio.Runner.close`. Correção operacional: executar somente os
  testes de integração fora dessa restrição; código e asserções permaneceram intactos.

**Verificação:** 13 testes PostgreSQL passaram; `orchestrator.db` com 94/94 statements
cobertos. Gate global: `913 passed, 2 skipped`, 4209/4209 statements (100%).

## D36 — Fase 2/T2: prompts em PostgreSQL (2026-07-18)

Segunda fatia da Fase 2 entregue sem alterar contratos HTTP nem frontend:

- A revisão Alembic `20260718_0002` cria `prompt_templates` e `prompt_last_used`, ambas
  com `organization_id`, FK para `organizations`, `FORCE ROW LEVEL SECURITY` e policy
  por `app.organization_id`. Templates usam identity transacional e índice
  `(organization_id, kind, id)` para listagem mais recente/filtro.
- `PostgresPromptRepository` implementa o mesmo contrato observável do JSON:
  save/list/delete de templates, validação de `creator`/`video` e upsert de `last_used`
  que preserva valores anteriores quando a entrada nova é vazia.
- `prompt_store.open_repository()` escolhe PostgreSQL quando `DATABASE_URL` existe e
  mantém `JsonPromptRepository` no modo mock/local. O vocabulário/validação comum ficou
  em `orchestrator.prompts`, sem acoplar o repositório SQL ao arquivo JSON.
- `GET/POST/DELETE /api/prompts` e `POST /api/run` passaram a usar o contrato assíncrono.
  O payload continua com `templates`, `last_used`, `store_path` e `exists`; no PostgreSQL,
  `store_path` vale `postgresql`. Nenhum arquivo em `front/**` mudou.
- A suíte limpa `DATABASE_URL` e as envs de tenant por default; somente testes de
  integração optam pelo PostgreSQL real, preservando hermeticidade offline.

### Red → Green e falhas investigadas

- RED inicial: `ModuleNotFoundError: orchestrator.db.prompts`. GREEN: migração 0002 e
  tracer end-to-end salvar → fechar pool → reabrir → listar template persistido.
- RED de exclusão: `PostgresPromptRepository` não possuía `delete_template`. GREEN:
  `DELETE ... RETURNING`, id inválido/inexistente retorna `False` e repetição é idempotente.
- RED de contexto recente: ausência de `get_last_used`/`record_last_used`. GREEN: tabela
  tenant-scoped com upsert por `(organization_id, kind)` e no-op para valores vazios.
- RED HTTP: com `DATABASE_URL` definido, `/api/prompts` ainda devolvia o caminho JSON e
  criava o arquivo-trap. Causa: rotas chamavam funções síncronas diretamente. Correção:
  seletor assíncrono único; o teste prova `store_path=postgresql` e nenhum JSON criado.
- Sintoma: cobertura focada inicial ficou em 97,89% com os 21 testes de prompts verdes.
  Causa: o comando focado não incluiu regressões legadas de JSON corrompido e atualização
  vazia em `tests/test_small_gaps.py`. Correção: incluir esses contratos no gate focado;
  stores JSON/PostgreSQL/domínio atingiram 147/147 statements (100%).

**Verificação focada:** 45 testes verdes, incluindo upgrade real `0001 → 0002`, restart,
fallback JSON, HTTP, ordenação/filtro, validação e RLS que bloqueia leitura/update entre
Acme e Globex. Gate global: `922 passed, 2 skipped`, 4300/4300 statements (100%).

## D36 — Fase 2/T3: creators em PostgreSQL + fronteira R2 (2026-07-19)

Terceira fatia da Fase 2 entregue sem alterar contratos HTTP nem frontend:

- A revisão Alembic `20260719_0003` cria `creators`, chaveada por
  `(organization_id, run_id, creator_id)`, com posição identity para ordenação
  determinística, metadata normalizada, `FORCE ROW LEVEL SECURITY` e policy por
  `app.organization_id`.
- `PostgresCreatorRepository` preserva record/list/find e o upsert do JSON: regravar o
  mesmo creator no mesmo run atualiza voz/status/metadata sem duplicar e o promove a
  mais recente. `JsonCreatorRepository` continua sendo o backend mock/offline.
- O PostgreSQL guarda somente metadata e ponteiros canônicos (`r2://bucket/key`).
  `/api/creators` deriva signed URLs de TTL curto na saída; ao reutilizar um creator,
  `/api/run` também assina o ponteiro para o handoff ao provider. Os testes reabrem o
  repositório depois dessas duas fronteiras e provam que nenhuma URL temporária voltou
  para o banco.
- O gate humano de creators passou a gravar pelo contrato assíncrono selecionado por
  `DATABASE_URL`. Sem essa env, o JSON, a recuperação de `/media` e o modo dry-run
  continuam inalterados. Nenhum arquivo em `front/**` mudou.

### Red → Green e falhas investigadas

- RED inicial: `ImportError` para `PostgresCreatorRepository`. GREEN: migração 0003,
  vocabulário normalizado e tracer persistir → fechar pool → reabrir → listar.
- Sintoma do primeiro GREEN focado: o comportamento passou, mas o comando saiu 1 com
  cobertura global em 28%. Causa: `pytest` aplica `--cov=orchestrator`/100% mesmo ao
  executar um único teste. Correção: ciclos unitários usam `--no-cov`; o gate global de
  cobertura continua obrigatório e foi executado ao final.
- RED de atualização: regravar `(run_id, creator_id)` levantava `UniqueViolation`.
  GREEN: `ON CONFLICT` atualiza campos, status e posição sem criar duplicata.
- RED HTTP/R2: `/api/creators` ainda devolvia o path JSON com `DATABASE_URL`. GREEN:
  selector assíncrono; a resposta recebe HTTPS assinado e a releitura mantém `r2://`.
- RED de reuso: `/api/run` buscava somente o JSON e respondia 404 para creator existente
  no PostgreSQL. GREEN: lookup no backend ativo e assinatura somente no handoff.
- RED de tenancy: sem a policy, a conexão Globex lia e atualizava a linha Acme. GREEN:
  `ENABLE/FORCE RLS`; a leitura expõe apenas Globex e o update cruzado afeta zero linhas.
- Sintoma do primeiro gate global: `929 passed, 2 skipped`, mas cobertura 99,61%
  (17 statements). Causa: faltavam os comportamentos de lookup do fallback JSON e
  recuperação/404 do finder assíncrono. Correção: testes públicos desses fluxos, sem
  excluir linhas ou afrouxar asserções.

**Verificação:** upgrade real `0002 → 0003` preservou tenant e prompts; persistência,
restart, upsert, ordenação, lookup, fallback JSON, RLS, signed URL na API e handoff ao
provider estão cobertos. Gate global: `931 passed, 2 skipped`, 4383/4383 statements
(100%).

## D36 — Fase 2/T4: feedback em PostgreSQL (2026-07-19)

Quarta fatia da Fase 2 entregue sem alterar contratos de CLI/runner nem frontend:

- A revisão Alembic `20260719_0004` cria `run_feedback`, chaveada por
  `(organization_id, run_id)`, com summary JSONB, posição identity para ordem de
  chegada determinística, `FORCE ROW LEVEL SECURITY` e policy por
  `app.organization_id`.
- `PostgresFeedbackRepository` preserva save/load/latest e o comportamento last-write-
  wins do JSON: regravar o mesmo run atualiza o summary, não duplica a linha e o torna
  o feedback mais recente.
- `feedback_store.open_repository()` seleciona PostgreSQL por `DATABASE_URL` e mantém
  `JsonFeedbackRepository` no modo mock/offline. O argumento `feedback_store` do runner
  e o `--feedback-store` da CLI continuam iguais; no PostgreSQL, o path não é criado.
- Tanto a leitura Step 10 → Step 1 em `runner.run_pipeline` quanto a gravação de
  `node_feedback` usam o mesmo contrato assíncrono. O segundo ciclo recebe exatamente
  os `winning_styles` persistidos pelo primeiro. Nenhum arquivo em `front/**` mudou.

### Red → Green e falhas investigadas

- RED inicial: `ImportError` para `PostgresFeedbackRepository`. GREEN: migração 0004
  e tracer save → fechar pool → reabrir → load do summary completo.
- RED de regravação: salvar novamente o mesmo run levantava `UniqueViolation`.
  GREEN: `ON CONFLICT` atualiza summary, timestamp e posição; `latest` retorna a última
  gravação.
- RED de tenancy: sem a policy, uma conexão Globex lia e atualizava a linha Acme.
  GREEN: `ENABLE/FORCE RLS`; a leitura raw expõe apenas Globex e o update cruzado afeta
  zero linhas.
- RED de wiring: `runner.run_pipeline` ainda criava o arquivo JSON mesmo com
  `DATABASE_URL`. GREEN: selector assíncrono único no runner e no node Step 10; o teste
  prova que o arquivo-trap não existe e o summary está no PostgreSQL.
- RED do loop: sem leitura do repositório, o segundo run recebia
  `prior_winning_styles=[]`. GREEN: `load_latest_feedback()` no backend ativo antes de
  montar o estado inicial.
- Sintoma do primeiro gate global: `937 passed, 2 skipped`, mas cobertura 99,93%
  (3 statements). Causa: faltavam os comportamentos `location`, `exists` e lookup por
  run da fachada JSON. Correção: teste de contrato do fallback, sem excluir linhas nem
  afrouxar o gate.

**Verificação:** 6 testes PostgreSQL cobrem restart, upsert/latest, RLS, pipeline, loop e
upgrade real `0003 → 0004` preservando creators; 30 regressões de feedback/loop/bias/CLI
passaram no JSON. Gate global: `938 passed, 2 skipped`, 4441/4441 statements (100%).

## D36 — Fase 2/T5: artifacts em PostgreSQL, bytes no R2 (2026-07-19)

Quinta fatia da Fase 2 entregue sem alterar nodes, contratos HTTP ou frontend:

- A revisão Alembic `20260719_0005` cria `artifacts`, chaveada por tenant e id
  determinístico, com unicidade de `(organization_id, storage_key)`, índices por run e
  expiração, JSONB para metadata e `FORCE ROW LEVEL SECURITY` por
  `app.organization_id`.
- `PostgresArtifactRepository` preserva o contrato completo do `ArtifactDB`: record/get,
  lookup por key/run, upsert idempotente, classificação de retenção, consulta de
  expirados e delete. `purge_expired` continua apagando bytes primeiro e metadata depois.
- O banco guarda somente metadata, proveniência e ponteiros canônicos
  (`storage_backend` + `storage_key`). Os bytes continuam no R2/local; `r2://` e URLs
  assinadas continuam derivados na fronteira de consumo e não viraram coluna canônica.
- `open_artifact_repository()` seleciona PostgreSQL por `DATABASE_URL`; sem ela cria e
  usa o mesmo SQLite offline. `run_pipeline` e `resume_pipeline` mantêm o pool aberto por
  todo o `ainvoke`, sem criar o arquivo SQLite quando PostgreSQL está ativo.
- `ArtifactRepository` formaliza o contrato comum e permite que `media_store`/nodes
  permaneçam agnósticos ao backend. Nenhum arquivo em `front/**` mudou.

### Red → Green e falhas investigadas

- RED inicial: `ImportError` para `PostgresArtifactRepository`. GREEN: migration 0005 e
  tracer record → fechar pool → reabrir → get de todos os campos canônicos.
- RED de idempotência: o repositório ainda não expunha `by_run`. GREEN: consulta ordenada
  e `ON CONFLICT` atualizam a mesma linha sem duplicar o ponteiro.
- RED de retenção/purge: faltavam `set_retention`, `by_key`, `expired` e `delete`.
  GREEN: o PostgreSQL implementa a mesma interface usada pelo QC e por `purge_expired`.
- Sintoma no teste de retenção: esperado `2026-07-22T12:00:00+00:00`, recebido o mesmo
  instante como `2026-07-22T09:00:00-03:00`. Causa: `timestamptz` foi renderizado no fuso
  da sessão PostgreSQL, divergindo do contrato textual determinístico do SQLite.
  Correção: normalização para UTC ao materializar `ArtifactRecord`; a asserção e o
  instante esperado foram preservados.
- RED de tenancy: antes da policy, uma conexão Globex lia as linhas Acme e atualizava o
  ponteiro cruzado. GREEN: `ENABLE/FORCE RLS`; SQL raw vê só Globex e update cruzado afeta
  zero linhas.
- RED de seleção: faltava `open_artifact_repository`, portanto não havia factory comum.
  GREEN: selector por `DATABASE_URL`, com restart real e prova de que o SQLite-trap não é
  criado.
- RED de wiring: o runner ainda instanciava `ArtifactDB` diretamente mesmo com
  PostgreSQL. GREEN: repositório injetado com lifetime envolvendo o grafo; uma escrita
  feita dentro de `ainvoke` sobrevive ao fechamento do pool.

**Verificação:** 8 testes PostgreSQL cobrem restart, upsert, retenção/purge, RLS,
seleção, lifetime no grafo e upgrade real `0004 → 0005` preservando feedback; 47
regressões locais de artifacts/retenção/grafo passaram. Gate global:
`946 passed, 2 skipped`, 4503/4503 statements (100%).

## D36 — Fase 2/T6: runs e read model durável (2026-07-22)

Sexta fatia da Fase 2 entregue sem trocar ainda o checkpointer LangGraph:

- A revisão Alembic `20260720_0006` cria `runs` e `run_items`, ambas tenant-scoped,
  com chaves/FKs compostas por organização, fases limitadas a `running`, `editing`,
  `awaiting`, `done` e `error`, índices de leitura e `FORCE ROW LEVEL SECURITY` por
  `app.organization_id`.
- `PostgresRunRepository` implementa start/save/get/list do read model. Cada `save` é
  snapshot exato e atômico: faz upsert dos items atuais e remove os ausentes; fase e
  shape mínimo são validados antes da transação. Recomeçar o mesmo id limpa summary,
  erro e projeções antigas.
- `run_store.open_repository()` liga PostgreSQL somente quando `DATABASE_URL` existe.
  Sem ela, execução, checkpoint SQLite e APIs locais preservam o comportamento anterior.
- `POST /api/run` registra o run antes de agendar a task. `_execute_run` mantém um único
  pool durante o ciclo e persiste progresso antes do SSE `item_update`, os gates de
  conceitos/creators, resultado final e erro terminal.
- `/api/runs`, `/api/status/{run_id}` e `/api/state/{run_id}` usam o read model como
  fallback quando `_runs` e o checkpoint local desaparecem. Os payloads pendentes dos
  gates também são reidratados depois de restart. Runtime/checkpoint continuam com
  precedência enquanto existem.
- Ponteiros canônicos `r2://` permanecem no PostgreSQL; signed URLs são produzidas apenas
  na fronteira HTTP. Duas organizações podem usar o mesmo `run_id` sem ler ou sobrescrever
  dados uma da outra.

Neste slice T6 o checkpointer ainda não havia migrado para `AsyncPostgresSaver`; essa
lacuna é fechada pelo T7 abaixo. A substituição de `BackgroundTasks`, `Future` dos gates
e buffer SSE em memória por jobs/eventos duráveis continua reservada à Fase 3.

### Red → Green e falhas investigadas

- Sintoma: PostgreSQL 16/`pg_ctl` não existiam no host; depois da extração dos pacotes,
  faltava `libpq`, e sockets foram bloqueados pelo sandbox. Causa: ambiente de validação
  sem servidor instalado e restrição de socket. Correção operacional: DEBs PostgreSQL 16
  e `libpq` extraídos em `/tmp`, `LD_LIBRARY_PATH` explícito e somente os testes de
  integração executados fora do sandbox; código e asserções permaneceram intactos.
- RED de snapshot: um item removido no estado mais novo continuava em `run_items`.
  Causa: `save` fazia apenas upsert. Correção: delete tenant-scoped dos ids ausentes na
  mesma transação.
- REDs de contrato: fase desconhecida vazava `CheckViolation` e item sem id vazava
  `KeyError`. Causa: validação dependia do SQL/dicionário. Correção: validar fase e shape
  antes de abrir a transação, com `ValueError` explícito.
- RED de restart: APIs devolviam 404 após limpar `_runs` e apagar o checkpoint; falhas
  ainda apareciam como `running`. Causa: leitura e exceção só atualizavam memória/SQLite.
  Correção: fallback PostgreSQL nas três rotas e persistência terminal `error` no handler.
- REDs dos gates: conceitos/creators continuavam `running`; após o primeiro fix, a fase
  voltava mas os payloads pendentes ficavam vazios. Causa: interrupções e `/api/state`
  só conheciam `_runs`. Correção: persistir `pending_concepts`/`pending_creators` no JSONB
  e reidratá-los normalizados no fallback.
- Sintoma no tracer de creators: o teste comparava o shape bruto inventado com a resposta
  pública normalizada. Causa: fixture não respeitava o contrato já coberto da API.
  Correção: usar aliases reais (`creator_id`, `image`, `voice`) e manter comparação exata
  com o payload normalizado; nenhuma asserção de produção foi afrouxada.
- RED de progresso: ao pausar a emissão de `item_update`, o banco ainda não continha o
  item. Causa: evento era emitido logo após atualizar memória. Correção: snapshot durável
  concluído antes da entrega SSE.
- RED de reuso: `start` do mesmo id preservava summary/items do run anterior. Causa:
  `ON CONFLICT` só atualizava parâmetros de entrada. Correção: reset do cabeçalho e delete
  atômico das projeções antigas.
- Sintoma durante refactor: a primeira integração abria um pool a cada evento persistido.
  Causa: selector aplicado ao redor de cada `save`. Correção: lifetime único envolvendo
  `_execute_run`, reutilizado por progresso, gates e término.
- Sintoma no build: `tsc` não existia na worktree; `npm ci --offline` foi bloqueado com
  `EPERM` ao validar o binário `esbuild`. Causa: dependências não materializadas e execução
  de binário restrita pelo sandbox. Correção operacional: instalação offline e build fora
  dessa restrição; lockfile e código frontend não mudaram.

**Verificação:** 18 testes PostgreSQL cobrem restart, snapshots, listagem/erro, validação,
gates, progresso antes do SSE, RLS, reset, upgrade real `0005 → 0006` e fronteira R2;
83 regressões web afetadas passaram. Gate global: `964 passed, 2 skipped`, 4626/4626
statements (100%). `npm run build`: TypeScript e Vite verdes (66 módulos).

## D36 — Fase 2/T7: checkpointer PostgreSQL tenant-scoped (2026-07-22)

Fatia T7 de persistência entregue; PostgreSQL agora também é a fonte do checkpoint
LangGraph quando `DATABASE_URL` está presente:

- `langgraph-checkpoint-postgres` 3.x entrou nas dependências e no `uv.lock`.
  `open_checkpointer()` seleciona `AsyncPostgresSaver`; sem URL mantém exatamente o
  `AsyncSqliteCompatSaver` offline e o `db_path` local.
- `orchestrator migrate`/`upgrade_database(..., revision="head")` executa o `setup()`
  oficial do saver e aplica `ENABLE/FORCE ROW LEVEL SECURITY` às três tabelas de dados.
  Requests e runners não executam DDL: apenas configuram o tenant da sessão e fazem DML.
- `TenantScopedPostgresSaver` preserva `thread_id = run_id` na interface LangGraph e
  adiciona o UUID da organização somente à chave física. `get`, `list`, `put`, writes,
  delete e delta history convertem a chave na fronteira, sem expor o prefixo ao grafo.
- As policies conferem esse prefixo físico contra `app.organization_id`. Portanto duas
  organizações podem reutilizar o mesmo `run_id`, e uma query SQL com o papel runtime
  continua vendo apenas o tenant configurado.
- `run_pipeline`, `resume_pipeline`, `get_status`, gates e execução web foram validados
  abrindo novas instâncias e até recebendo `db_path` diferentes: o estado é retomado do
  PostgreSQL e nenhum arquivo SQLite é criado.
- `/api/status/{run_id}` continua usando o read model de T6 quando existe projeção
  durável, mas não existe checkpoint (por exemplo, erro antes do primeiro super-step).

Com T7, runs, items, prompts, creators, feedback, artifacts e checkpoints sobrevivem a
restart no PostgreSQL, com modo mock local separado. A Fase 2 permanece aberta até o
importador legado idempotente copiar e conferir SQLite, JSON e mídia local no PostgreSQL/R2.
Jobs, outbox, gates sem `Future` e SSE persistido começam somente na Fase 3.

### Red → Green e falhas investigadas

- RED de restart: a segunda instância, usando outro `db_path`, retornou snapshot sem
  `run_id`. Causa: o selector ainda abria SQLite mesmo com `DATABASE_URL`. Correção:
  `AsyncPostgresSaver` com o serializer atual; reabrir a conexão recupera o mesmo estado
  sem criar os arquivos-trap.
- RED de tenancy: Globex leu todo o checkpoint Acme ao consultar o mesmo `run_id`.
  Causa: as tabelas oficiais usam apenas `thread_id`/namespace e não conhecem organização.
  Correção: wrapper profundo que compõe a chave física com organization UUID e devolve o
  `run_id` original em todos os configs públicos.
- RED de RLS: uma conexão Globex contou 20 linhas Acme em `checkpoints`. Causa: o wrapper
  isolava a API, mas faltava a segunda barreira SQL. Correção: `FORCE RLS` em checkpoints,
  blobs e writes, policies pelo prefixo tenant e `app.organization_id` na sessão do saver.
- Sintoma web: `permission denied for schema public` ao abrir `/api/state`. Causa:
  `open_checkpointer()` chamava `setup()` em toda leitura, exigindo CREATE do papel runtime.
  Correção: setup/policies movidos para `migrate`; runtime permaneceu somente DML.
- Sintoma no primeiro setup migratório: `PostgresSaver.from_conn_string()` rejeitou
  `serde=`. Causa: na versão instalada, o context manager síncrono não aceita esse kwarg;
  serializer é necessário para leitura/escrita async, não para criar schema. Correção:
  setup síncrono sem serializer e saver async com `_serde()` preservado.
- Sintoma no primeiro gate global: 99% de cobertura, com lifecycle do wrapper e fallback
  de status sem regressão. Causa: os fluxos principais usavam get/put, mas não list/delete/
  delta nem read-model-only. Correção: testes públicos de lifecycle e status; nenhum ramo
  foi excluído e a cobertura voltou a 100%.

**Verificação:** matriz PostgreSQL com 67 testes; 114 regressões focadas de checkpoint,
resume, CLI e web. Gate global: `970 passed, 2 skipped`, 4700/4700 statements (100%).
`npm run build`: TypeScript e Vite verdes (66 módulos).

## D36 — Fase 2/T8: import legado idempotente (2026-07-25)

A Fase 2 foi fechada com um importador explícito e reiniciável:

- `scan_legacy()` valida SQLite/JSON/mídia e produz manifesto e checksum determinísticos
  sem escrever. `orchestrator import-legacy` é dry-run por padrão; `--apply` exige
  `DATABASE_URL`, resolve o storage por `--config-dir` e usa o tenant de `ORCH_*`.
- A revisão `20260722_0007` registra batches/entries tenant-scoped com RLS. O mesmo
  checksum vira `noop`; drift da origem é recusado; erro persistido vira `failed` e pode
  ser retomado. Um advisory lock PostgreSQL serializa `(organization, source_id)`.
- Checkpoints/runs, prompts, creators, feedback e artifacts são copiados para os
  repositórios PostgreSQL. Bytes vão ao backend de mídia sob keys tenant-scoped; imagem
  e preview de voz do creator viram ponteiros canônicos, enquanto `voice_ref` opaco é
  preservado.

### Red → Green e falhas investigadas

- Sintoma: o teste nem iniciou com `python: No such file or directory`. Causa: a worktree
  não ativa o venv automaticamente. Correção operacional: usar `.venv/bin/python`.
- Sintoma: a fixture tentou a porta 5432 e depois deixou `pytest_db_tmpl` ao ser
  interrompida. Causa: faltavam os parâmetros do PostgreSQL externo e o janitor não
  tolera template ausente/parcial. Correção operacional: host/porta/banco explícitos e
  remoção somente de `pytest_db`/`pytest_db_tmpl` confirmados como descartáveis.
- Sintoma: Alembic não autenticou como admin e o pool runtime rejeitou a senha. Causa:
  a URL construída pela fixture omitia credenciais; no caso runtime o teste havia acabado
  de provisionar uma senha diferente. Correção: senha runtime explícita na URL do teste e
  contêiner descartável alinhado ao contrato `trust` dos papéis temporários da suíte.
- RED de assets: só um objeto era enviado ao storage. Causa: creators eram gravados antes
  de materializar `image`/`voice_preview`. Correção: upload determinístico e substituição
  apenas de `image_uri`/`voice_preview_uri`; três objetos e `voice_ref` preservado.
- RED de tenancy: o import sempre criava `legacy-local`. Correção:
  `TenantIdentity.from_env()`, comprovado com outro tenant.
- RED de recuperação: batch permanecia `pending` após falha de storage. Correção: coluna
  `error`, transições `pending → failed → pending → applied` e retry do mesmo checksum.
- RED concorrente: duas tasks retornaram `applied` e duplicaram uploads. Correção: lock
  advisory de sessão mantido por toda a aplicação; a segunda execução observa `applied`
  e retorna `noop`.
- RED CLI: `--apply` ainda abortava como indisponível. Correção: wiring de Database,
  storage e importador, saída JSON uniforme para `applied`/`noop`.

**Verificação focada:** 14 testes do importador e 87 testes do gate PostgreSQL/CLI
verdes. Gate global: `990 passed, 2 skipped`, 4998/4998 statements (100%).

## D36 — Fase 3: jobs, gates, SSE e efeitos duráveis (2026-07-25)

A API PostgreSQL deixou de executar o grafo em `BackgroundTasks`: `POST /api/run`
confirma, numa transação, `run` + job determinístico + eventos + outbox. O Runner
`--once` reivindica com `FOR UPDATE SKIP LOCKED`, lease de 120 s e heartbeat de 30 s;
recupera lease vencida, aplica retry exponencial limitado e encerra em `failed`.
SQLite, `_runs`, `Future` e o SSE em memória permanecem somente no modo local offline.

- Interrupts agora viram `run_gates` versionados; a decisão atômica rejeita versão stale,
  cria exatamente um job `resume_run` e o Runner retoma com `Command(resume=...)`.
- `run_events` fornece sequência monotônica, frames SSE com `id:` e replay por
  `Last-Event-ID`. Eventos públicos (`run_start`, gates, `run_end`, `error`) continuam
  compatíveis com `EventSource.onmessage`; reinício de API não perde o stream.
- A outbox trata Cloudflare Queues, SQS ou sweep PostgreSQL como wake-up, nunca como
  verdade do job. Falha de publicação volta a `pending` com backoff e, após cinco
  tentativas, entra em `failed` como DLQ operacional.
- `provider_quotas` serializa consumo global e `external_effects` reserva custo por
  chave de negócio antes da chamada. Duplicata devolve o resultado persistido;
  resultado `uncertain` nunca é reemitido automaticamente. Adapters pagos permanecem
  bloqueados no v1 salvo opt-in explícito.
- Creators reutilizados permanecem canônicos (`r2://`) no job. A URL temporária nasce
  somente no Runner, imediatamente antes do handoff ao provider. Decisões de creator
  são persistidas no repositório durante o resume.

### Red → Green e falhas investigadas

- RED inicial: `PostgresEffectLedger` não existia. Correção: migration `0008`, ledger
  transacional e testes de idempotência, quota concorrente, resultado e estado incerto.
- RED SQL: o claim falhou com `column reference "id" is ambiguous`. Causa: `UPDATE ...
  FROM candidates RETURNING id` não qualificava a tabela. Correção: colunas qualificadas
  no retorno do claim.
- Sintoma: o teste SQS ficou preso ao executar `asyncio.to_thread`. Causa: o scheduler
  real não é apropriado ao stub síncrono do contrato. Correção: fronteira async
  injetável, mantendo `to_thread` como default de produção.
- RED de heartbeat: `run_worker_once` não aceitava intervalo e o lease não era renovado.
  Correção: task de heartbeat cancelada de forma segura ao terminar o executor.
- RED de perda de lease: o heartbeat falhava, mas o executor continuava até um timeout
  externo, abrindo janela de execução concorrente. Correção: corrida explícita entre
  heartbeat e executor; perder o lease cancela imediatamente o trabalho.
- RED de restart/UI: `/api/runs` ficou sem `active` após limpar `_runs`, e o SSE usava
  `event:` nomeado, invisível ao `onmessage` atual. Correção: fases vêm do read model e
  os frames persistidos usam `id:` + `data:` com tipos públicos.
- RED de mídia: a API assinava o creator antes de gravar o job; ao mover a assinatura,
  o primeiro Runner falhou com `default_media_path` ausente. Correção: job canônico,
  import explícito e resolução no consumo.
- RED de outbox: a primeira falha deixava a entrada em `publishing` até expirar o lease.
  Correção: transição explícita `publishing → pending/failed`, erro persistido e backoff.
- Regressão nos testes da Fase 2: cenários ainda esperavam conclusão imediata por
  `BackgroundTasks` e gates por `Future`. Causa: contrato antigo corretamente
  substituído. Correção: os mesmos casos agora acionam Runner, gate/version e resume
  duráveis; nenhuma asserção de integridade foi removida.
- RED de segurança/custo: um job com `config/providers.yaml` ainda alcançava o executor
  live. Correção: o Runner recusa papéis não-mock sem
  `ORCH_ENABLE_PAID_ADAPTERS=true`.
- Primeiro gate global: 41 linhas novas sem exercício deixaram cobertura em 99,26%.
  Correção: casos públicos de erro/lease, transições ambíguas, factories de fila,
  polling/replay SSE e compatibilidade local; nenhum ramo foi excluído.

**Verificação focada:** 35 testes de jobs/efeitos/filas verdes; migration real
`0007 → 0008` preserva runs. Gate global: `1028 passed, 2 skipped`, 5552/5552
statements (100%).

## D36 — Fase 4: staging Cloudflare/Neon (2026-07-25)

O staging foi especificado como código sem acionar provider pago nem publicar
infraestrutura real:

- `config-staging/` mantém todos os adapters de geração em `mock` e move somente bytes
  para R2. A imagem OCI Linux/amd64 é a mesma para API e Runner, com comandos distintos.
- O Worker TypeScript serve a SPA com fallback, encaminha `/api/*` e SSE sem guardar
  estado, preserva o JWT do Cloudflare Access e injeta somente o contexto de organização.
  O callback da Queue chama o Runner interno; cron de um minuto cobre a recuperação.
- `CloudflareAccessMiddleware` valida RS256 contra o JWKS oficial, issuer e audience.
  O sujeito validado entra em `TenantIdentity`, mas a autorização final exige membership
  preexistente no PostgreSQL; não há bootstrap implícito em tráfego autenticado.
- A administração explícita ganhou `db org-create`, `db membership-grant` e
  `db membership-revoke`. O `runner-service` expõe somente health e uma chamada
  autenticada que drena uma outbox e reivindica no máximo um job durável.
- OpenTofu fixa Cloudflare `~> 5.22` e Neon `~> 0.1.15`: R2 privado/CORS restrito,
  wake queue + DLQ, Access com MFA, WAF/rate limit, DNS e PostgreSQL 16 em
  `aws-sa-east-1`, branch protegida e history retention de sete dias.
- O workflow de deploy constrói uma única imagem por SHA, publica o mesmo artefato no
  registry Cloudflare e ECR, migra/provisiona o papel runtime antes do rollout gradual e
  nunca usa tag `latest`. `docs/STAGING.md` documenta bootstrap, rollback e o requisito
  de conexão direta (sem pooler) para migrações e checkpoints.

### Red → Green e falhas investigadas

- RED inicial: `ModuleNotFoundError: jwt`. Causa: a validação Access não tinha biblioteca
  JOSE. Correção: `PyJWT[crypto]` no runtime e lock atualizado.
- RED seguinte: `orchestrator.auth` e `orchestrator.runner_service` inexistentes.
  Correção: middleware ASGI e serviço interno implementados pelas interfaces públicas
  exercitadas nos testes.
- Sintoma: o parser estático de `wrangler.jsonc` corrompia `https://` ao remover `//` e
  asserções dependiam de whitespace/forma textual. Causa: teste acoplado à representação.
  Correção: JSON sem comentários foi lido semanticamente e HCL/workflow passaram a usar
  regex/contratos observáveis.
- Sintoma TypeScript: bindings de `cloudflare:workers` e secrets não eram conhecidos.
  Correção: tipos gerados pelo Wrangler e declaração separada dos secrets, sem conflito
  com a lib WebWorker.
- Sintoma de segurança: `npm audit` encontrou três vulnerabilidades altas transitivas em
  `sharp`/`miniflare` no Wrangler 4.112. Correção: Wrangler 4.114; audit voltou a zero.
- Sintoma IaC: `tofu fmt -check` rejeitou o alinhamento do ruleset. Correção: formatação
  canônica; `init` gerou lockfile e `validate` passou com a imagem oficial
  `ghcr.io/opentofu/opentofu:1.12.1`.
- Primeiro gate global: comportamento funcional verde (`1047 passed, 2 skipped`), mas
  cobertura em 99,15%. Causa: ramos reais de falha/refresh do JWT, pool/lifespan e
  administração ainda não tinham regressão. Correção: testes pelos endpoints e comandos
  públicos; nenhum ramo foi excluído nem asserção afrouxada.

**Verificação:** `npm run check`, `npm audit` (zero vulnerabilidades),
`wrangler deploy --dry-run`, `tofu fmt -check` e `tofu validate` verdes. Gate global:
`1058 passed, 2 skipped`, 5797/5797 statements (100%).

## D36 — Fase 5: operação, backup e segurança (2026-07-25)

- `ORCHESTRATOR_LOG_FORMAT=json` produz JSON Lines UTC com logger, nível, mensagem e
  correlação por `run_id`, `job_id`, `organization_id`, `provider` e evento. O staging
  ativa esse formato; LangSmith continua opt-in por env.
- `PostgresOperations.inspect_run()` e `orchestrator ops inspect-run RUN_ID` reconstroem
  read model, items, jobs/leases, gates/resoluções, eventos ordenados, artifacts, efeitos,
  bytes e custo exclusivamente das fontes duráveis e sob RLS.
- `health_snapshot()` agrega fila/outbox, leases expiradas, lag de stream, erros de
  assinatura, quotas e gasto. Alertas têm códigos estáveis: `expired_job_lease`,
  `outbox_dlq`, `storage_signing_error`, `stream_lag`, `provider_limit` e
  `anomalous_spend`.
- `orchestrator ops maintain` executa purge orientado por metadata, inventário via
  `HeadObject`/`exists` e health no mesmo contexto tenant. O workflow diário agenda
  manutenção, cria dump PostgreSQL custom, calcula SHA-256, restaura em PostgreSQL 16
  vazio e só então arquiva dump/checksum no R2 privado.
- `docs/OPERATIONS.md` fixa RPO <= 5 min pelo PITR Neon, RTO <= 60 min, resposta a alertas,
  restore, inventário e exercícios trimestrais de carga/restart/SSE/isolamento.

### Red → Green e falhas investigadas

- RED de logs: a saída continuava textual mesmo com `ORCHESTRATOR_LOG_FORMAT=json`.
  Correção: formatter JSON central e staging configurado para ativá-lo.
- RED de reconstrução: `orchestrator.operations` inexistente. Correção: read model
  operacional tenant-scoped; o tracer reúne todas as fontes de um run e custo/bytes.
- Sintoma no primeiro teste CLI: `asyncio.run()` foi chamado dentro do loop do próprio
  teste. Causa: teste async dirigindo uma interface Click síncrona. Correção: setup async
  concluído antes da invocação, preservando o contrato real da CLI.
- RED de inventário/manutenção: não havia interface para conferir ponteiros nem agendar
  purge. Correção: inventário derivado do PostgreSQL e comando `ops maintain`; bytes são
  apagados antes da metadata.
- Primeiro exercício de restore: `pg_dump` falhou porque a fixture já havia removido
  `pytest_db`. Correção operacional: banco origem descartável explícito, migração real,
  dump, restore em segundo banco e remoção somente desses dois alvos.

**Verificação:** 18 testes focados verdes; `orchestrator.operations` 85/85 statements
(100%); TypeScript verde e `npm audit` sem vulnerabilidades. O exercício Docker restaurou
o dump e leu `alembic_version=20260725_0008` antes de remover os bancos descartáveis.
Gate global: `1066 passed, 2 skipped`, 5925/5925 statements (100%).

## D36 — Fase 6: exercício AWS e cutover verificável (2026-07-26)

- O contrato de mídia agora aceita `s3` e `dual`. Durante o cutover, novas escritas vão
  ao backend configurado e assinatura, inventário, retenção e purge continuam roteados
  pelo `storage_backend` original de cada artifact.
- `storage migrate-run RUN_ID` copia a key canônica R2→S3, preserva content type e
  metadata, verifica SHA-256/tamanho via `HeadObject` e só então troca o ponteiro no
  PostgreSQL. Repetição é idempotente; divergência mantém o artifact no R2.
- `sqs-runner` recebe o wake-up SQS, mas reivindica e executa o job canônico no
  PostgreSQL. Falha de processamento não confirma a mensagem, preservando retry/DLQ.
- `infra/aws-staging` declara ECR imutável, ECS/Fargate API+Runner, ALB, SQS+DLQ, S3
  privado/versionado, IAM mínimo, logs e alarmes. API e Runner permanecem com
  `desired_count=0`; o workflow exige decisão explícita e não foi aplicado.
- `docs/AWS-CUTOVER.md` fixa drenagem, leitura dual, migração verificável, canário e gate
  Go/No-Go sem mudar `run_id`, `storage_key`, checkpoints ou eventos.
- O frontend migrou para React 19.2.8 e React Router 8.3.0, com Node 22.22.3 na imagem
  OCI. O pacote legado `react-router-dom` foi removido e todos os imports usam
  `react-router`.
- Estado/segredos/planos OpenTofu locais (`*.tfstate*`, `*.auto.tfvars`, `*.tfplan`) são
  ignorados; o workflow salva o plano com extensão coberta por essa política.

### Red → Green e falhas investigadas

- Sintoma no primeiro gate global: dois testes legados de signed URL deixaram ponteiros
  R2 sem resolução. Causa: o roteamento dual passou a exigir que todo signer anunciasse
  `.backend`, mas o contrato R2 legado expunha apenas `get_signed_url`. Correção: signer
  simples sem marcador continua sendo tratado como R2, enquanto ponteiro S3 nunca é
  assinado pelo backend errado; regressões cobrem ambos os casos.
- Sintoma de segurança: `npm audit` reportou duas vulnerabilidades altas em
  `react-router@7.18.1` (GHSA-qwww-vcr4-c8h2). Causa: a correção existe apenas em
  `react-router@8.3.0`; o pacote `react-router-dom` foi removido na v8 e o novo baseline
  exige React >=19.2.7 e Node >=22.22.0. Correção: migração direta para
  `react-router@8.3.0`, React/ReactDOM 19.2.8 e imagem Node 22.22.3; audit voltou a zero,
  sem `npm audit fix --force`.
- RED de segurança IaC: o teste de contrato provou que `.terraform/` era ignorado, mas
  state, `auto.tfvars` e plano salvo ainda podiam entrar no Git. Correção: padrões
  explícitos no `.gitignore` e plano `aws-no-traffic.tfplan`; o mesmo teste ficou verde.

**Verificação:** 72 testes focados verdes. Frontend com build Vite/TypeScript,
boundaries e audit zero; Cloudflare com TypeScript, audit zero e
`wrangler deploy --dry-run`; OpenTofu 1.12.1 com `fmt -check` e `validate`. A imagem
`ugc-orchestrator:d36` Linux/amd64 expôs CLI, `storage migrate-run` e `sqs-runner`, com
Python 3.12.13 e Node 22.22.3. Gate global PostgreSQL:
`1092 passed, 2 skipped`, 6131/6131 statements (100%).

**Estado externo:** nenhuma infraestrutura Cloudflare/AWS foi aplicada, nenhum DNS ou
publisher foi trocado e nenhum provider pago foi chamado.

## D30 — R2 + DB relacional de mídia: implementação (2026-07-16)

Execução da `docs/ADR-D30-media-storage-r2-db.md`, que estava aceita mas não implementada.
Escopo travado com o usuário: **só a D30** (storage + DB), SQLite-first, atrás de config.
Hospedar o app na Cloudflare ficou **fora** — ver "Cloudflare" abaixo.

### Fases entregues
- **Fase 1 (`b345136`)** — contrato `MediaStorage` (`put_bytes`, `put_from_url`,
  `get_signed_url`, `delete`, `exists`) + `LocalMediaStorage`. Toda escrita devolve
  `StoredObject` (backend, key, uri, content_type, size_bytes, sha256). `media_store`
  virou orquestração por cima: decide *o que* persistir e sob qual key canônica; o
  backend decide *onde*. URIs servíveis inalteradas.
- **Fase 2 (`a913a01`)** — `ArtifactDB` (SQLite) com as colunas mínimas da ADR. `id`
  determinístico (`sha256` de `run_id:storage_key`), não `uuid4` → `record()` idempotente.
- **Fase 3 (`78b943a`)** — `R2MediaStorage` (boto3, S3-compatible), backend selecionável
  por `providers.yaml` (`storage.backend`), coberto com stub de S3.
- **Fase 3.5 (`66e4cc3`)** — o elo que faltava: as Fases 1-3 eram infra sem consumidor.
  `runner._build_config` resolve storage + DB uma vez por run (como o adapter) e os nodes
  passam adiante via `_persistence()`.
- **Fase 5 (`696e450`)** — retenção: `keep` / `rejected` (3d) / `intermediate` (2d),
  `purge_expired` orientado pelo DB.
- **Fase 4** — signed URLs sob demanda: `resolve_signed_uris` troca `r2://{bucket}/{key}`
  por URL assinada (TTL 900s) **só na saída** de `/api/state/{run_id}` e `/api/creators`.

### Decisões de desenho
- **`aiosqlite` trava neste ambiente** (já documentado em `graph/checkpoint.py`), então o
  `ArtifactDB` usa `sqlite3` síncrono sob lock com fachada async — mesmo padrão, mesmo
  motivo. Já o R2 usa `asyncio.to_thread`: upload de vídeo segurando o event loop mataria
  o fan-out paralelo de items.
- **Retenção só é decidível depois do fato.** Quando o clip é persistido, o QC ainda não
  rodou. `classify_item_retention` roda no veredito: aprovado → última take `keep`,
  anteriores `intermediate`; drop → todas `rejected`. Item ainda em voo não é classificado.
- **`storage_key` carimbado no `meta` do Artifact.** Sem ele, quem está a jusante teria de
  reconstruir a key a partir da uri — impossível no R2 (`r2://bucket/key`) e dependente de
  adivinhar a extensão.
- **`kind` vem do próprio `Artifact`** (`clip`/`video`), não de um vocabulário paralelo: o
  modelo de estado já carregava essa informação (descoberto quando o pydantic recusou um
  `Artifact` sem `kind` num teste meu).
- **Falhar alto**: backend desconhecido em `providers.yaml` e credencial R2 ausente
  levantam no boot, em vez de degradar para disco local (mídia paga em disco efêmero) ou
  quebrar no meio de um run pago. **Exceção deliberada**: `_signing_storage` engole config
  quebrada e devolve `None` — o run já falhou alto no boot, mas cegar o dashboard tiraria
  justamente a tela onde o operador lê o erro.
- **Assinar é transformação de saída, nunca mutação.** `resolve_signed_uris` devolve
  cópia. Se escrevesse a URL de volta no estado, o checkpoint passaria a guardar uma URL
  vencida como se fosse o ponteiro — exatamente o que a D30 proíbe. Cada key é assinada
  uma vez por payload (o mesmo clip aparece em `results` e em `artifacts`).
- **`r2://` é renderável para a UI**, porque vira https assinado na saída; os demais
  schemes (`s3://`, `gs://`) seguem sendo referência opaca.

### Falhas investigadas (sintoma → causa → correção)
- **Teste próprio com `RecursionError` de monkeypatch.** Sintoma: `transport` duplicado em
  `test_put_from_url_uses_its_own_client...`. Causa: a lambda que substituía
  `httpx.AsyncClient` chamava `httpx.AsyncClient` — já era ela mesma. Correção: guardar a
  classe real antes do patch, idioma que `test_gateway_llm.py` já usava. Bug do teste, não
  do código.
- **Inserção duplicada ao ligar os call sites.** Sintoma: `**_persistence(...)` duplicado
  num call site. Causa: substituição textual com padrão de 8 espaços que é **substring**
  do de 12 espaços. Correção: remoção manual + `ast.parse` como gate antes de rodar.

### Cloudflare (por que o app não foi para lá)
A D30 é sobre **onde os bytes moram**, não sobre hospedagem — R2 é S3-compatible e serve
de qualquer host. Hospedar *este* app na Cloudflare esbarra em: **Python Workers** roda
Pyodide/Wasm (langgraph, pillow e o SDK anthropic não têm wheel PyEmscripten); **Containers**
é viável mas tem **disco efêmero**, então o checkpointer SQLite e a mídia local
evaporariam — exigiria DB durável, que a própria D30 põe em *fora de escopo* ("trocar
SQLite por Postgres nesta etapa"). Seria uma ADR nova (compute/DB), não a D30.

**Critérios de aceite da ADR — todos atendidos:** `config-mock` offline/determinístico/sem
custo; suíte verde sem credenciais R2; bytes no R2 + metadata no DB no perfil live; signed
URLs sob demanda e não persistidas; reprovados 3 dias; intermediárias 2 dias; creator
assets, aprovados e finais sem expiração automática.

**Escopo mantido fora:** migrar artifacts existentes, Postgres e purge agendado seguem
fora, como a própria ADR define.

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **884 passed, 2 skipped**,
cobertura 100% (era 772). Dirigido fora da suíte, em dois níveis:
1. Run mock de batch 4 gravou 18 artifacts reais no DB com key canônica, content_type,
   size_bytes e sha256 — 8 `intermediate` com `expires_at` em +2 dias e 10 `keep` sem
   expiração, com a última take de cada item retida.
2. Caminho **live** inteiro com stub de S3 (sem credencial): bytes no bucket, linha no DB
   com `backend=r2`, estado guardando `r2://ugc-prod/run-1/items/item-0/clip-0.mp4`, UI
   marcando `renderable=True`/`media_type=video`, API servindo
   `https://acct.r2.cloudflarestorage.com/...?X-Amz-Expires=900&X-Amz-Signature=...` — e
   estado e DB **inalterados** depois de assinar.

## D30 — Fase 6: SSE assina e R2 ligado de verdade (2026-07-16)

Bucket `generation-video` provisionado e as `R2_*` configuradas, então `config/providers.yaml`
passou a `storage: backend: r2`. `config-mock` segue `local` — dry-run continua offline,
determinístico e sem custo.

**Conectividade verificada contra o R2 real** (fora da suíte): `put_bytes` → `exists` →
`get_signed_url` com GET 200 e bytes idênticos → `delete`. `list_buckets` dá `AccessDenied`
e isso é o esperado: o token é escopado a um bucket só, e ListBuckets é permissão de conta.

**Fase 4 fechada — o SSE agora assina.** `stream_events` resolve `r2://` no `yield`, não no
`_emit`. O ponto é o buffer de replay: ele é reenviado a quem conecta tarde, então guardar a
URL assinada nele entregaria uma URL já vencida. Assinando na saída, o buffer mantém o
ponteiro canônico e o TTL só começa a correr quando o evento chega ao cliente. O backend de
assinatura é construído **uma vez por stream** (era um `boto3.client` por evento).

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **888 passed, 2 skipped**,
cobertura 100%.

**Não verificado:** o bucket ser privado. O GET sem assinatura devolve 400, mas isso não
prova nada — o endpoint da API S3 rejeita qualquer requisição não assinada de qualquer jeito.
Quem decide acesso público é o Public Development URL (`r2.dev`) no dashboard, que precisa
ser confirmado desabilitado. Um batch pago end-to-end também não foi rodado.

## D35 — Persona antes de conceitos, scripts e creator (2026-07-16)

Objetivo: adicionar uma persona batch-level antes de qualquer conceito, reutilizada como
contexto em concepts/scripts e como briefing do creator, preservando dry-run offline,
determinismo e execução agentic via typed tools.

### Red → Green (TDD)
- RED inicial: `tests/test_persona.py` falhava com `ModuleNotFoundError` para
  `orchestrator.tools.persona`, `KeyError: 'write_persona'` no registry e ausência de
  `MockAdapter.write_persona`/`CompositeAdapter.write_persona`.
- GREEN:
  - `LLMPort.write_persona`, `write_persona_tool`, `ToolSpec(write_persona)` e delegação
    `CompositeAdapter.write_persona`.
  - `MockAdapter`, `GatewayLLMAdapter` e `AnthropicLLMAdapter` implementam persona; Gateway
    e Anthropic streamam com stage `persona`.
  - Top graph agora roda `persona -> concepts -> scripts -> concept_review -> roster`.
  - `BatchState.persona` é salvo; persona é passada para concepts/scripts e prefixa o
    `creator_prompt` sem alterar o prompt seguro de imagem.
  - `agent_catalog` permite `persona`; `config/agents.yaml` usa `executor: agent` e
    `config-mock/agents.yaml` usa `executor: tool`.
  - Backend/frontend exibem `Persona` na timeline.
- Continuação D35: cada stage agentic atual (`persona`, `concepts`, `scripts`, `video`)
  agora declara `target_agent` e `system_prompt_path`; o loader concatena
  `prompts/agents/_shared.md` + prompt do stage, valida arquivo ausente/vazio e expõe
  apenas `system_prompt_path`/`has_system_prompt` no catálogo. O texto resolvido é passado
  internamente para `run_stage_agent` em Mock, Gateway e Anthropic.

### Falhas investigadas nesta fase
- Sintoma: após inserir persona, a suíte completa quebrou em
  `test_feedback_loop_biases_next_cycle` (`share2 == 1`).
  - Causa: o mock distribuía o viés entre todos os estilos vencedores
    (`bias[i % len(bias)]`); com a persona no hash, o top winner do ciclo anterior podia
    receber só um slot enviesado.
  - Correção: slots enviesados do mock agora privilegiam `bias[0]`; slots não enviesados
    continuam preservando spread determinístico.
- Sintoma: gate de cobertura caiu para 99,81% em `AnthropicLLMAdapter.write_persona`.
  - Causa: os ramos novos de streaming e refusal da persona não estavam cobertos.
  - Correção: adicionar regressões offline para streaming stage `persona` e refusal.
- Sintoma: ao adicionar `system_prompt` ao `AgentPort`, a suíte completa falhou em
  `tests/test_video_agent_node.py` com `_MultiTakeAdapter.run_stage_agent() got an
  unexpected keyword argument 'system_prompt'`.
  - Causa: o fake de vídeo no teste ainda implementava a assinatura antiga do port.
  - Correção: atualizar o fake para aceitar o kwarg opcional e manter a simulação de
    múltiplas takes inalterada.
- Sintoma: cobertura caiu para 99,95% em `agent_catalog.py`.
  - Causa: os ramos de `system_prompt_path` inválido e prompt sem `_shared.md` eram novos
    e ainda não exercitados.
  - Correção: adicionar regressões para path traversal e prompt stage-only.

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **772 passed, 2 skipped**,
cobertura 100%. `cd front && rtk npm run build` → build Vite/TypeScript limpo.


## Caminho A — tool layer foundation (2026-07-14)

Objetivo: entregar a primeira fundação do Caminho A sem `AgentRuntime`: o LangGraph
continua coordenando a pipeline, mas os nodes agora chamam tools tipadas que delegam
para o `CompositeAdapter` já resolvido em `RunnableConfig`.

### Red → Green (TDD)
- RED: `tests/test_tools.py` especificou o novo pacote `orchestrator.tools`, o
  `ToolContext`, validações de shape (`ToolOutputError`), trace markers offline e a
  delegação dos nodes para tools. A primeira execução falhou com
  `ModuleNotFoundError: No module named 'orchestrator.tools'`.
- GREEN:
  - `src/orchestrator/tools/`: `base.py`, `concepts.py`, `scripts.py`,
    `creators.py`, `video.py`, `qc.py`, `assembly.py` e `registry.py`.
  - Tools são finas: recebem `ToolContext`, adicionam metadata mínima de tracing,
    chamam o método correspondente do adapter e validam o output antes de devolver.
  - `nodes/stages.py` trocou chamadas diretas a adapter por
    `generate_concepts_tool`, `write_script_tool`, `build_creator_tool`,
    `generate_clip_tool`, `qc_check_tool`, `assemble_video_tool` e
    `upscale_video_tool`, preservando persistência de mídia, SSE, gates humanos,
    seed creator e fallback de assembly.

### Falha investigada nesta fase
- Sintoma: após criar as tools, `tests/test_tools.py` ainda falhava em um caso de
  erro claro.
  - Causa: bug no teste; o texto esperado `non-empty list[dict` foi usado como regex
    sem escapar `[` e o pytest rejeitou o padrão.
  - Correção: usar `re.escape(expected_shape)` no `match`.
- Sintoma: a primeira suíte completa passou todos os testes funcionais, mas falhou no
  gate de cobertura: `total of 99 is less than fail-under=100`.
  - Causa: `nodes/base.py::get_adapter` virou dead code depois da troca para
    `ToolContext`; dois ramos de erro dos validators novos ainda não eram exercitados.
  - Correção: remover `get_adapter` e adicionar testes explícitos para `Artifact` com
    `uri` vazia e QC output não-mapping.

Verificação: `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_tools.py
tests/test_stages_coverage.py tests/test_builder.py tests/test_registry_composite.py`
→ 73 passed; `rtk proxy .venv/bin/python -m pytest` → 596 passed, 2 skipped,
cobertura 100%; `rtk proxy env LANGSMITH_TRACING=false LANGSMITH_API_KEY=
.venv/bin/orchestrator run --batch 1 --offer "serum X" --config-dir config-mock`
→ dry-run mock aprovado (1 produzido, 1 aprovado).

## Caminho A — Fase 2 registry agentic (2026-07-14)

Objetivo: transformar `TOOL_REGISTRY` de lista estatica minima em contrato publico
interno para roteamento agentic futuro, ainda sem ligar agent execution em runtime.

### Red → Green (TDD)
- RED: `tests/test_tools.py` passou a exigir `function_path`, `target_model`,
  `target_agent`, `agent_enabled`, `capabilities`, helpers de lookup/resolucao e uma
  prova de que as tools importadas por `nodes/stages.py` estao registradas.
- GREEN:
  - `ToolSpec` ganhou os campos agentic opcionais com defaults compativeis.
  - Cada spec declara `function_path` importavel e capabilities declarativas.
  - `registry.py` expoe `get_tool_spec`, `tool_specs_for_stage` e
    `resolve_tool_function`.
  - Os testes validam que `function_path` resolve para a funcao real e que o trace marker
    continua `tool.{name}`.

### Falha investigada nesta fase
- Sintoma: os testes novos falharam com `AttributeError: 'ToolSpec' object has no
  attribute 'function_path'` e `ImportError` para os helpers do registry.
  - Causa raiz: o registry ainda era apenas metadata documental; nao havia caminho
    importavel nem API de consulta para agentes ou catalogo futuro.
  - Correção: expandir o contrato do `ToolSpec`, preencher paths/capabilities das tools
    reais e adicionar helpers de lookup/resolucao sem mudar os nodes.
  - Verificação: `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_tools.py -q`
    → 25 passed; `rtk proxy .venv/bin/python -m pytest` → 601 passed, 2 skipped,
    cobertura 100%.

## Caminho A — Fase 3 catálogo agents/models (2026-07-14)

Objetivo: adicionar configuração declarativa de executor/model por stage/tool sem mudar
topologia LangGraph e sem ligar agents em runtime.

### Red → Green (TDD)
- RED: `tests/test_agent_catalog.py` exigiu `load_agent_catalog`, default compatível
  quando `agents.yaml` falta, merge de overrides, validação de stage/tool/executor,
  serialização estável, arquivos oficiais em `config/` e `config-mock/`, e injeção do
  catálogo no runner/CLI/web. `tests/test_web_spa.py` passou a exigir `agents` em
  `/api/integrations` preservando `stages`.
- GREEN:
  - `orchestrator.agent_catalog` define `AgentCatalog`, `StageExecutionSpec`,
    `default_agent_catalog()` e builder validado a partir de YAML.
  - `config/agents.yaml` e `config-mock/agents.yaml` declaram todos os stages em
    `executor: tool`, com `agent_enabled: false`.
  - `load_agent_catalog` cai para default quando o arquivo falta, mantendo config-dirs
    antigos compatíveis.
  - Runner, CLI e web passam `agent_catalog` dentro de `RunnableConfig.configurable`;
    nodes ainda não usam esse dado.
  - `/api/integrations` agora retorna `{"stages": ..., "agents": ...}`.

### Falhas investigadas nesta fase
- Sintoma: `load_agent_catalog` não existia; depois `config/agents.yaml` e
  `config-mock/agents.yaml` também não existiam.
  - Causa raiz: a Fase 2 tinha apenas o registry; a configuração declarativa ainda não
    havia sido introduzida.
  - Correção: criar o módulo/loader e os dois arquivos oficiais.
- Sintoma: `stages: []` passava como catálogo válido.
  - Causa raiz: `data.get("stages") or {}` mascarava lista vazia inválida como mapping
    vazio.
  - Correção: tratar apenas campo ausente/null como default; tipos inválidos levantam
    `ValueError`.
- Sintoma: um parametrized quebrou na coleta com 3 valores para 2 nomes.
  - Causa raiz: literal YAML separado por vírgula no teste.
  - Correção: concatenar a string corretamente.
- Verificação: fatia focada `tests/test_agent_catalog.py tests/test_web_spa.py
  tests/test_cli.py tests/test_web_endpoints.py` → 78 passed; suíte completa
  `rtk proxy .venv/bin/python -m pytest` → 619 passed, 2 skipped, cobertura 100%.

## Caminho A — Fases 4-6 executor agentic opt-in (2026-07-14)

Objetivo: concluir a trilha D29 com um executor configuravel `tool`/`agent`, piloto
offline em `concepts`/`scripts`, e decisao operacional para manter midia fora de agent
execution.

### Red → Green (TDD)
- RED: `tests/test_stage_executor.py` exigiu `orchestrator.stage_executor`, modo `tool`,
  modo `agent`, validacao de tools permitidas, erro para stage ausente e pipeline mock
  completa com `concepts`/`scripts` em agentic opt-in. `tests/test_agent_catalog.py`
  passou a exigir que `agent_enabled` e `executor` sejam consistentes e que stages de
  midia nao possam usar `agent`.
- GREEN:
  - `execute_stage_tool` passou a ser a fronteira entre nodes e tools.
  - Todos os nodes que chamam tools passam pelo executor.
  - Modo `tool` chama a tool diretamente; modo `agent` adiciona trace
    `agent.stage_executor`, valida catalogo e chama a mesma tool, mantendo validators.
  - `concepts` e `scripts` aceitam `executor: agent` + `agent_enabled: true`.
  - `video`, `roster`, `qc`, `assembly` e `upscale` ficam bloqueados para `agent`.

### Falhas investigadas nesta fase
- Sintoma: testes novos falharam com `ModuleNotFoundError` para
  `orchestrator.stage_executor`.
  - Causa raiz: a Fase 3 só carregava catálogo; não havia executor runtime.
  - Correção: criar `stage_executor.py` e integrar os nodes.
- Sintoma: a suíte completa passou funcionalmente, mas quebrou cobertura em
  `stage_executor.py`.
  - Causa raiz: o ramo de erro para stage ausente no catálogo não estava coberto.
  - Correção: adicionar regressão explícita para `StageExecutionError`.
- Sintoma: o catálogo permitia configurações ambíguas e mídia agentic.
  - Causa raiz: `executor` e `agent_enabled` eram aceitos independentemente; não havia
    allowlist dos stages LLM-only.
  - Correção: exigir `executor: agent` junto de `agent_enabled: true` e limitar agentic a
    `concepts`/`scripts`.
- Verificação: `rtk proxy .venv/bin/python -m pytest` → 629 passed, 2 skipped, cobertura
  100%; `rtk proxy .venv/bin/python -m compileall -q src tests` → OK.

Estado em **2026-07-06**. Suíte: **537 passando, 2 skips** (testes `--live` opt-in,
pulados sem `JUDGE_GATEWAY_URL`) + 2 warnings conhecidos/benignos (LangSmith
deprecation em import; LangGraph resume parcial — ver falha #5).
Cobertura: **100%** com gate `fail_under=100` (ver seção abaixo).
Rodar: `rtk proxy python -m pytest`.

> Nota: a falha que estava em aberto em `_SAFE_CREATOR_PROMPT` foi corrigida — ver falha #10.

## Cobertura completa de testes + gate fail_under=100 (2026-07-06)

Objetivo: fechar as lacunas de cobertura (91% → **100%**) e travar um gate permanente
que quebra o pytest se a cobertura cair. Os buracos estavam concentrados nos **adapters
reais** e nos **caminhos de erro/streaming** que os `MockAdapter` do v1 não exercitam.

Como foram testes de **caracterização** (o código já existia, verde na 1ª passada), a
proteção é contra regressão futura. Tudo offline/determinístico: bridge Node e downloads
via monkeypatch de `asyncio.create_subprocess_exec`/`httpx`; branches de "client próprio"
dos adapters HTTP cobertos capturando o construtor real de `httpx` **antes** de patchar o
módulo (evita recursão, já que `module.httpx` é o módulo global compartilhado).

Arquivos de teste novos/estendidos: `test_replicate_video.py`,
`test_vercel_seedance_assembly.py`, `test_anthropic_llm.py`, `test_tracing.py`,
`test_stages_coverage.py` (novo), `test_web_endpoints.py` (novo), `test_small_gaps.py`
(novo), `test_creator_real.py`, `test_judge_eval.py`, `test_checkpoint.py`, `test_cli.py`.

Config: `addopts` passou a incluir `--cov=orchestrator --cov-report=term-missing` e
`[tool.coverage.report]` com `fail_under = 100` + `exclude_lines` para os pragmas.

### Achado (dead code) durante a caracterização

- `adapters/replicate_video.py::_coerce_output`: o guard interno `if not value:` para uma
  chave de vídeo que é lista é **inalcançável** — o `if value:` acima já garante lista
  não-vazia. Marcado `# pragma: no cover` (não é comportamento errado, é defesa morta).

### `# pragma: no cover` adicionados (ramos genuinamente inatingíveis neste ambiente)

- `tracing.py` L27-28 — `except` do import do `langsmith` (a lib está instalada).
- `vercel_seedance_assembly.py` — `ImportError` do Pillow (instalado).
- `replicate_video.py` — o guard morto acima.
- `cli.py` — `except ImportError` do uvicorn (é dep `[web]` instalada) e o
  `if __name__ == "__main__"` (só roda via `python -m`).

Esses pragmas são o que torna `fail_under=100` **atingível e estável**. Verificação:
`rtk proxy python -m pytest` → `Required test coverage of 100.0% reached`, 537 passed.

## Retry de 429 nos adapters HTTP puros do creator + erro claro em shape inesperado (2026-07-06)

Sintoma: `tests/test_creator_real.py` trazia 6 testes RED sem GREEN correspondente —
`OpenAIImageAdapter`, `TopazUpscaleAdapter` e `ElevenLabsVoiceAdapter` não aceitavam
`backoff_base` e não retentavam `429`; além disso, um `generate_face` com shape
inesperado (sem `primary`/`angles`) estourava `KeyError` cru em `build_creator`.

Causa raiz: esses três adapters (contrato HTTP direto, sem SDK Replicate) nunca
ganharam a mesma política de retry aplicada aos adapters Replicate
(`replicate_upscale.py`/`replicate_video.py`/`replicate_voice.py`) quando o rate
limiting foi introduzido (falha #14) — o trabalho ficou pela metade (só os testes
foram escritos).

Correção: os três adapters passaram a envolver a chamada HTTP em
`with_transport_retry` (mesmo módulo `_retry.py`, mesma semântica: retenta só
`429`, backoff exponencial determinístico via `backoff_base`/`max_retries`
injetáveis). `RealCreatorAdapter.build_creator` valida `primary`/`angles` no dict
devolvido por `generate_face` e levanta `RuntimeError` com mensagem explícita antes
de indexar. Verificação: `rtk proxy python -m pytest` → **415 passed, 2 skipped**.

## Prompts persistidos no servidor + redesign do fluxo do dashboard (2026-07-03)

Objetivo: acabar com prompts que "somem" — templates viviam só no `localStorage`
do browser, o botão "Salvar Prompts" do modal apenas fechava o overlay, e o prompt
do run só era persistido no servidor como carona do creator aprovado (nunca quando
`approve_creators=false`). Também melhorar o fluxo do form (seções 1·Produto /
2·Prompts / 3·Executar, com os prompts ativos visíveis antes de iniciar).

### Red → Green (TDD)
- RED: `tests/test_web_prompts.py` (18 casos) exigiu `orchestrator/prompt_store.py`
  (save/list/delete de templates + `record_last_used`/`get_last_used`),
  `default_prompt_store_path()` (`ORCH_PROMPTS`, default `.orchestrator/prompts.json`),
  endpoints `GET/POST /api/prompts` e `DELETE /api/prompts/{id}`, registro do
  último prompt usado em todo `POST /api/run`, e contratos estáticos da UI
  (templates via DOM, rascunho persistente, `applyPrompts`, chips de status,
  reuso de prompts do histórico).
- GREEN:
  - `prompt_store.py` (novo): JSON com `templates` (`_idx` incremental p/ ordenação
    determinística, padrão do `creator_store`) + `last_used` por tipo (`creator`/`video`).
  - `web/server.py`: endpoints acima; `start_run` grava `last_used` sempre.
  - `web/static/index.html`:
    - Templates agora carregam do servidor e são montados via `createElement`/
      `textContent` + `addEventListener` (helper `buildTemplateCard`); os 6 templates
      builtin saíram do HTML inline para `BUILTIN_TEMPLATES` (JS data).
    - Rascunho das textareas em `localStorage` (`draft_*_prompt`), restaurado no load;
      sem rascunho, cai no `last_used` do servidor. "Salvar Prompts" → `applyPrompts()`.
    - Form principal em 3 seções com chips (`#prompt-status`) mostrando o prompt
      ativo de creator/vídeo antes de gastar créditos.
    - Histórico ganhou "↩ Reusar prompts" (preenche o builder com os prompts do run).
    - `migrateLocalTemplates()` sobe templates legados do `localStorage` para o
      servidor uma única vez.

### Falha investigada nesta fase (raiz do "prompt salvo não aplica")
- Sintoma: clicar num template salvo às vezes não preenchia a textarea (silencioso).
  - Causa raiz: `loadCustomTemplates` injetava o prompt num atributo `onclick`
    inline escapando apenas `'` e `\n`; qualquer prompt com **aspas duplas**
    quebrava o atributo HTML e o clique virava no-op. `title`/`desc` também
    entravam por `innerHTML` sem escape.
  - Correção: cards montados via DOM com `textContent` e listener; o texto do
    template nunca passa por parsing de HTML. Regressão:
    `test_ui_templates_pane_is_dom_built_without_inline_injection` + smoke live
    (template com aspas duplas e quebra de linha salva/aplica/deleta via API).
- Verificação: `rtk proxy python -m pytest` → **391 passed, 2 skipped**;
  `node --check` no script extraído do HTML; smoke com uvicorn +
  `POST/GET/DELETE /api/prompts` e `GET /` → 200.

## Correção — imagem de referência do Seedance acima de 30 MiB (2026-07-02)

Sintoma: o assembly final falhava no Vercel AI Gateway/Seedance com
`The request failed because the size of the input image (31 MiB) exceeds the limit (30 MiB)`.

Causa raiz: o fan-out guardava só a URL remota da imagem upscalada (`image_source_uri`) no
`Item`. Algumas imagens upscaladas pelo Replicate ficavam com 31-38 MB; o bridge Node
enviava essa referência diretamente para `experimental_generateVideo`, e o Gateway
rejeitava o input por tamanho.

Correção: `persist_creator_media` agora guarda também `image_local_path`; o fan-out propaga
esse path para `Item.creator_image_local_path`; o `VercelSeedanceAssemblyAdapter` prefere o
arquivo local no assembly e comprime qualquer referência acima do alvo seguro (28 MiB) para
um JPEG temporário antes de chamar o bridge. Para checkpoints antigos sem path local, o
adapter baixa a URL remota para temporário e aplica a mesma compactação. Regressões em
`tests/test_media_store.py`, `tests/test_builder.py` e `tests/test_vercel_seedance_assembly.py`.

## Perfil live sem mock + assembly Seedance 2.0 (2026-07-02)

Objetivo: fazer `config/` representar o caminho live real, sem `mock` nos papéis runtime,
e gerar o vídeo final com Seedance 2.0 via Vercel AI Gateway. `config-mock/` continua
sendo o dry-run determinístico/offline.

### Red → Green (TDD)
- RED: novos testes exigiram `config/providers.yaml` sem `mock` em `llm`/`creator`/
  `video`/`qc`/`assembly`, `video.allow_mock_fallback=false`, `IntegrityQCAdapter`,
  `VercelSeedanceAssemblyAdapter` e erro explícito em tier não-LTX quando o fallback do
  Replicate estiver desligado.
- GREEN:
  - `adapters/integrity_qc.py`: bloqueia mídia mock/fallback e URIs que não sejam vídeo.
  - `adapters/vercel_seedance_assembly.py`: monta payload para `bytedance/seedance-2.0`,
    com runner Node injetável e saída `data:video/mp4`.
  - `scripts/vercel_generate_video.mjs` + `package.json`: bridge para AI SDK
    `experimental_generateVideo`.
  - `nodes/stages.py`: QC/assembly recebem o `Item` completo; assembly recebe prompt final
    com script, conceito e briefing do run.
  - `registry.py` e `config/`: registram `integrity_qc`/`vercel_seedance_assembly` e
    desabilitam fallback mock no vídeo live.

### Falha investigada nesta fase
- Sintoma: a fatia focada falhava na coleta com `ModuleNotFoundError` para
  `orchestrator.adapters.integrity_qc` e `orchestrator.adapters.vercel_seedance_assembly`.
  - Causa: os testes RED especificavam adapters ainda inexistentes; `config/` ainda
    apontava `qc`/`assembly` para `mock`.
  - Correção: criar os adapters, bridge Node, contratos por `Item` e atualizar registry/config.
  - Verificação: `rtk proxy python -m pytest` → **366 passed, 2 skipped**;
    `rtk node --check scripts/vercel_generate_video.mjs` → OK.

## Remoção completa de distribuição do motor (2026-07-02)

Objetivo: tirar postagem/agendamento do produto. O motor agora termina em `assembly`;
um item aprovado/finalizado é aquele com `assembled` preenchido, ou `dropped=True` se
esgotou o QC.

### Red → Green (TDD)
- RED: testes passaram a exigir ausência do node `distribution` no item graph e feedback
  contando aprovados por `assembled`.
- GREEN:
  - `graph/builder.py`: `assembly -> END`; removido node/edge de distribuição.
  - `graph/state.py`: removido `Item.distributed`.
  - `adapters/base.py`, `adapters/mock.py`, `registry.py`: removidos `DistributionPort`,
    `distribute` e role `distribution`.
  - `runner.py`, `nodes/stages.py`, `web/server.py`, UI: conclusão por `assembled`.
  - `config/*.yaml`, docs e testes atualizados para a pipeline sem distribuição.
  - Verificação final: `rtk proxy python -m pytest` → **357 passed, 2 skipped,
    2 warnings conhecidos**.

### Falha investigada nesta fase
- Sintoma: a fatia focada falhava em
  `tests/test_builder.py::test_item_graph_has_expected_nodes` porque `distribution`
  ainda existia, e em `tests/test_feedback_store.py::test_node_feedback_writes_to_store`
  porque `approved` ainda era calculado por `distributed`.
  - Causa: o grafo e o feedback ainda usavam a semântica antiga de postagem.
  - Correção: remover Step 9 e trocar estado terminal aprovado para `assembled`.

## ElevenLabs via Replicate no creator live (2026-07-02)

Objetivo: garantir que o perfil live use **somente ElevenLabs** para TTS, mantendo o
hosting/execução pelo Replicate. Antes, `creator_real_replicate` usava
`ReplicateVoiceAdapter`, mas o modelo default era `suno-ai/bark`, contrariando a regra de
produto.

### Red → Green (TDD)
- RED: `tests/test_replicate_voice.py` passou a exigir `REPLICATE_ELEVENLABS_MODEL`,
  input `text` por padrão e campos configuráveis para schema/voice/model do ElevenLabs
  no Replicate. `tests/test_creator_real.py` prova que `creator_real_replicate` injeta
  esse adapter configurado. `tests/test_retry.py` já expunha retry faltante para
  `httpx.HTTPStatusError` 429.
- GREEN:
  - `adapters/replicate_voice.py`: removido default Bark/Suno; modelo ElevenLabs via
    Replicate agora é obrigatório por env ou `model=`, com input configurável por env.
  - `adapters/creator_real.py`: factory `creator_real_replicate` mantém Replicate para
    upscale e usa `ReplicateVoiceAdapter` configurado para ElevenLabs.
  - `adapters/_retry.py`: 429 de `HTTPStatusError` agora é retentável como throttle
    transitório; 401/422/etc. continuam propagando na primeira tentativa.
  - `.env.example`, README, decisões e mindmap atualizados para documentar
    `REPLICATE_ELEVENLABS_MODEL` e remover Bark/Suno do caminho live.

### Falha investigada nesta fase
- Sintoma: `rtk proxy python -m pytest` falhava em
  `tests/test_retry.py::test_retries_on_http_429_then_succeeds`.
  - Causa: `_retry.py` documentava retry para `httpx.HTTPStatusError` 429, mas
    `_is_retryable` só tratava `httpx.TransportError` e `ReplicateError(status=429)`.
  - Correção: incluir `HTTPStatusError` com `response.status_code == 429` como
    retentável, preservando propagação imediata para status não-429.

## Paridade áudio↔imagem do creator + reroll com gênero travado (2026-07-02)

Objetivo: garantir que a **voz** do creator case com a **imagem** gerada. Antes, imagem
(GPT Image, texto livre) e voz (preset inferido por keyword) não compartilhavam nenhuma
decisão de gênero — podiam divergir; e o roster reusava um `creator_prompt` único para os
N creators (vozes uniformes, imagens variando). Também fechamos a lacuna de teste do reroll.

### Red → Green (TDD)
- `adapters/base.py`: `assign_voice_profile(system_prompt, voice_profile, *, index)` —
  perfil **concreto** (nunca `None`): override > inferência > gênero determinístico por
  índice (alterna `female`/`male`). `image_gender_clause(profile)` — frase brand-safe de
  gênero para o prompt de imagem. Testes: `tests/test_voice_profile.py`.
- `adapters/openai_image.py`: `generate_face`/`_build_creator_image_prompt` aceitam
  `voice_profile` e injetam a cláusula de gênero (só male/female; neutral/None → sem cláusula).
- `adapters/creator_real.py`: `build_creator` resolve o perfil **antes** da imagem e passa
  o mesmo perfil a `generate_face` e a `create_voice` → paridade por construção. Testes
  em `test_creator_real.py` provam que imagem e voz recebem o mesmo perfil.
- `adapters/mock.py`: o preset resolvido também entra no seed do SVG (paridade em nível de
  metadado, offline/determinístico). Testes em `test_adapters_mock.py`.
- `nodes/stages.py`: `node_roster` chama `assign_voice_profile(creator_prompt, None, index=i)`
  por creator e repassa a `build_creator`. `reroll_creator_voice` reconstrói o `VoiceProfile`
  persistido (`_creator_voice_profile`), passa-o ao método do adapter (quando existe) e
  **trava o gênero** no reroll (só a amostra muda); preview fallback é seedado com o preset.
- Lacuna fechada: `tests/test_stages_reroll.py` (novo, 6 casos) cobre os dois branches
  (adapter com método vs. fallback), preservação de imagem e de gênero, incremento do
  contador, determinismo e sensibilidade do preview ao preset.

### Falha investigada nesta fase
- Sintoma: spies de `build_creator`/`generate_face` em `test_system_prompt.py` e
  `test_creator_real.py` quebraram com `unexpected keyword argument 'voice_profile'`.
  - Causa: a nova assinatura (contrato) passou a repassar `voice_profile` do roster ao
    adapter e deste à imagem; os fakes seguiam a assinatura antiga.
  - Correção: fakes atualizados para aceitar `voice_profile` (mudança **intencional** de
    contrato, não afrouxamento) — asserções de `system_prompt` mantidas e reforçadas com
    a paridade imagem↔voz.

## Voice persona em adapters de creator/voice (2026-07-01)

Objetivo: suportar reroll determinístico de voz do creator com preset
`male|female|neutral` e briefing humano curto, mantendo compatibilidade quando nenhum
perfil for informado.

### Red → Green (TDD)

- RED: `tests/test_replicate_voice.py`, `tests/test_creator_real.py` e
  `tests/test_adapters_mock.py` passaram a exigir um contrato `VoiceProfile`,
  inferência determinística a partir de `system_prompt`, override explícito com
  precedência e repasse do perfil pelo `RealCreatorAdapter` até o sub-adapter de voz.
- GREEN:
  - `adapters/base.py`: novo `VoiceProfile`, helpers `infer_voice_profile` /
    `resolve_voice_profile`, `CreatorPort.build_creator(..., voice_profile=None)` e
    `VoicePort.create_voice(..., voice_profile=None)`.
  - `adapters/mock.py`: `voice_id` / `voice_preview_uri` passam a derivar também do
    perfil resolvido; mock segue offline e determinístico.
  - `adapters/replicate_voice.py`: prompt legado (`creator voice {index}`) preservado
    quando não há perfil; com perfil, inclui preset + briefing humano.
  - `adapters/elevenlabs_voice.py`: request opcionalmente inclui `description` e
    `labels.preset`.
  - `adapters/creator_real.py`: resolve o perfil de voz a partir de texto ou override,
    repassa ao sub-adapter e expõe `voice_profile` no payload do creator.

### Falha investigada nesta fase

- Sintoma: os testes novos nem coletavam, com `ImportError: cannot import name
  'VoiceProfile' from orchestrator.adapters.base`.
  - Causa: o slice de contratos ainda não expunha nenhum tipo/helper comum para voz,
    então cada adapter seguia com assinatura antiga (`create_voice(index)`).
  - Correção: centralizar o contrato em `adapters/base.py` e propagar a assinatura
    opcional de `voice_profile` só pelo slice de adapters.

## Correção — histórico recupera creators quando `creators.json` está vazio (2026-07-01)

Sintoma: o modal de histórico do frontend não mostrava creators antigos; `/api/creators`
lia `.orchestrator/creators.json`, mas o arquivo local estava zerado (`{}`), enquanto a
mídia antiga ainda existia em `.orchestrator/media/<run_id>/creator-*`.

Causa raiz: o histórico dependia exclusivamente do JSON de store. Se o arquivo fosse
apagado/reescrito vazio ou configurado para um caminho sem dados, a UI não tinha fallback
para a mídia já persistida.

Correção: `/api/creators` mantém o store como fonte primária, mas quando ele não tem
entradas reconstrói um histórico básico a partir de `ORCH_MEDIA`/`.orchestrator/media`,
preferindo imagens renderizáveis (`image.png`/`image.svg`/etc.) e previews de voz
(`voice.wav`/`voice.mp3`/etc.), marcando os itens como `status: recovered`.
Regressão: `test_creators_history_recovers_from_media_when_store_is_empty`.

## Fase vídeo Replicate LTX 2.3 sem áudio (2026-07-01)

Objetivo: ligar o role `video` ao Replicate real usando LTX 2.3 Fast, primeiro sem
áudio. Áudio/voiceover e concatenação ficam para a próxima etapa.

Sintoma: `ReplicateVideoAdapter` ainda usava um contrato REST manual/fictício
(`/predictions` com `model` e `output`) e não seguia o padrão já corrigido de
`ReplicateUpscaleAdapter`/`ReplicateVoiceAdapter` (`replicate.async_run` + retry). Além
disso, o grafo não carregava a imagem do creator para o stage de vídeo.

Causa raiz: D14 provou o papel `video` com httpx injetável antes dos adapters Replicate
migrarem para SDK oficial. Depois que upscale/voz passaram para SDK, vídeo ficou com um
contrato divergente e sem dados de creator suficientes para image-to-video.

Correção: `ReplicateVideoAdapter` agora usa `replicate.async_run` com runner injetável,
retenta via `with_transport_retry`, força `generate_audio: false` e normaliza outputs
`str`/`list`/`dict`. O tier `ltx` usa `lightricks/ltx-2.3-fast`; `kling`/`seedance`
ficam em fallback mock com `fallback_reason` até refs reais existirem. `Item` ganhou
`creator_image_uri`; o fan-out preenche esse campo a partir do roster; os nodes de vídeo
montam prompt com `video_prompt`, script e conceito e passam a imagem ao adapter.

### Red → Green (TDD)
- RED: `tests/test_replicate_video.py`, `test_fan_out_attaches_creator_image_uri_from_roster`
  e os testes de `make_gen_node`/`node_product_demo` falharam por ausência de `runner`,
  `creator_image_uri` e `reference_image_uri`.
- GREEN: implementação mínima nos adapters, state/fan-out e nodes. Focado:
  `rtk proxy python -m pytest tests/test_replicate_video.py tests/test_builder.py::test_fan_out_attaches_creator_image_uri_from_roster tests/test_system_prompt.py::test_gen_node_passes_video_prompt tests/test_system_prompt.py::test_node_product_demo_passes_video_prompt -q`
  → 11 passed.

## Fase retry de throttle 429 do Replicate (2026-07-01)

Sintoma: em produção, `upscale`/`voz` do creator falhavam com
`ReplicateError status: 429 — Request was throttled` porque a conta tinha < $5 de
crédito (rate limit reduzido a 6 req/min, **burst 1**) e os creators paralelos
disparavam upscale+voz simultâneos. Como upscale/voz são best-effort
(`creator_real.py`), a pipeline não quebrava mas perdia upscale (caía pra imagem
original) e voz (ficava vazia).

Causa raiz: `with_transport_retry` (`_retry.py`) só retentava `httpx.TransportError`;
o `429` vinha como `replicate.exceptions.ReplicateError` e propagava na 1ª tentativa,
apesar de ser transitório ("resets in ~Ns").

Correção: `_retry.py` agora trata como retentável também `ReplicateError` com
`status == 429` (helper `_is_retryable`), mantendo backoff exponencial determinístico;
outros status HTTP (422/500) e erros de lógica seguem propagando na hora.

### Red → Green (TDD)
- `tests/test_retry.py` (novo, 4 casos): retry em 429 até suceder; exaustão em 429
  persistente; não-429 (422) propaga na 1ª; `TransportError` segue retentado.

Nota operacional: a correção mitiga, mas não elimina o throttle — a solução de raiz é
crédito ≥ $5 no Replicate (remove o burst-1). Um semáforo limitando a concorrência do
fan-out de creators fica como melhoria futura.

## Fase streaming/render de mídia — escutar & visualizar (2026-07-01)

Objetivo: fazer o dashboard mostrar ao vivo o roteiro, a imagem, a voz **tocável** e o
vídeo **tocável**, com detalhe por item e progresso por estágio — funcionando tanto no run
real quanto na demo mock offline. Entregue em 4 workstreams (backend persistência, mock
renderável, redesign de UI, integração), delegados a agentes Sonnet sob TDD.

### Red → Green (TDD)

- **Persistência de mídia de item + voz audível** (`media_store.py`, `nodes/stages.py`,
  `adapters/elevenlabs_voice.py`):
  - `persist_item_media` baixa `clips[].uri`/`assembled.uri` http(s) para
    `/videos/{run_id}/items/{item_id}/…` (provenance em `meta["source_uri"]`); no-op para
    `mock://`/opaco. Chamado nos gen nodes e em `node_assembly`.
  - `elevenlabs_voice.synthesize_preview` gera amostra TTS curta; `_build_voice_preview`
    resolve um `voice_preview_uri` audível (reusa voz já baixada do Replicate; sintetiza
    para id opaco ElevenLabs; preserva preview já emitido pelo adapter). `voice_preview_uri`
    passou a sair nos eventos `creator_ready` e `approve_creators`.
  - Regressões: testes em `test_creator_real.py` (`synthesize_preview`, `persist_item_media`,
    `_build_voice_preview`).
- **Mock renderável** (`adapters/mock.py`): URIs `mock://` → `data:` determinísticas e
  renderáveis — `data:image/svg+xml` (creator), `data:audio/wav` (voice_preview_uri),
  `data:video/mp4` (clips/assembled) com um mp4 válido/tocável de 932 bytes compartilhado,
  variação por item via fragmento `#hash` (browser ignora no decode; mantém o teste
  `test_generate_clip_with_prompt_uri_differs`). Regressões em `test_adapters_mock.py` e
  `test_system_prompt.py`.
- **Redesign de UI** (`web/static/index.html`): `itemsMap`/`creatorsMap` como estado
  canônico com merge incremental; drawer de detalhe por item (player de vídeo, galeria de
  imagem, áudio de voz via join `item.creator_ref → creator`, roteiro, QC); `assembled`
  agora é `<video>` (com fallback de poster quando não tocável) e não texto; barras de
  progresso por estágio e barra global do batch; feed de tokens LLM estilizado como prosa
  do roteiro; voz tocável no painel de aprovação e no creator strip.

### Falhas investigadas nesta fase

- Sintoma: na demo mock, a voz do creator chegava à UI como `voice_preview_uri: null`,
  apesar do MockAdapter emitir um `data:audio/wav` válido — sem voz audível offline.
  - Causa: `_build_voice_preview` (backend) retornava `None` quando o adapter não expõe
    `.voice.synthesize_preview` (caso mock), e `node_roster` **sobrescrevia**
    incondicionalmente `creator["voice_preview_uri"]` com esse `None`, apagando o preview
    que o adapter já havia setado.
  - Correção: `_build_voice_preview` passou a preservar um `voice_preview_uri` já presente
    no creator antes de qualquer síntese/reuso.
  - Regressão: `test_roster_creator_ready_carries_renderable_voice_preview`
    (`tests/test_web_item_updates.py`).

### Correção pós-review — histórico não mostrava imagem/referências dos creators

- Sintoma: no modal Histórico (e no creator strip), a imagem do creator aparecia em
  branco/quebrada e as referências (voz, oferta, prompts) não eram visíveis.
  - Causa 1 (imagem): a imagem mock é `data:image/svg+xml`, mas `_EXT_BY_MIME` em
    `media_store.py` não mapeava `image/svg+xml` → o arquivo era persistido como
    `image.bin` e servido pelo StaticFiles como `application/octet-stream`, que o browser
    não renderiza em `<img>`. (Confirmado por smoke: `GET /media/.../image.bin -> 200
    content-type=application/octet-stream`.)
  - Correção 1: adicionado `image/svg+xml: "svg"` ao mapa e fallback via
    `mimetypes.guess_extension` antes de degradar para `.bin` — agora persiste `image.svg`,
    servido como `image/svg+xml`. Regressão: `test_persist_media_data_uri_svg_keeps_svg_extension`.
  - Causa 2 (referências): `renderHistory` só mostrava id + imagem + status; voz, oferta e
    prompts ficavam apenas no `title` (tooltip).
  - Correção 2 (`web/static/index.html`): card de histórico agora exibe player de voz
    (`<audio>` quando `voice_preview_uri` é audível), e referências visíveis (oferta, voz,
    ângulos, prompts, run). Novo helper `renderableAudioUri`. Card alargado p/ acomodar.
- Lightbox de imagem (`web/static/index.html`): clicar em qualquer imagem de creator
  (histórico, strip, painel de aprovação, galeria do drawer, poster de vídeo) amplia em tela
  cheia; fecha via ✕, clique no fundo ou Esc. Helpers `openLightbox`/`closeLightbox`/
  `makeExpandable`.
- Diagnóstico "imagens não aparecem" (ambiente do usuário): o store `.orchestrator/creators.json`
  continha entradas obsoletas — `mock://…` (runs com o mock antigo, pré-`data:`) e
  `/media/…/image.bin` (runs desta sessão, anteriores à correção do svg). Ambas não
  renderizam. Runs novos (após restart do servidor p/ carregar o Python novo) persistem
  `image.svg` renderável — confirmado por smoke: `GET /media/…/image.svg -> 200 image/svg+xml`.
  Nota: `config/providers.yaml` usa `creator: creator_real_replicate` por padrão, então o
  botão "start" do dashboard roda o creator REAL (custo/keys); para demo offline use um
  config-dir all-mock.

### Verificação final

- `rtk proxy python -m pytest` → **272 passed, 2 skipped, 2 warnings**.
- Smoke end-to-end offline (servidor + run mock via HTTP/SSE, config all-mock): `creator_ready`
  com `voice_preview_uri` `data:audio/wav` renderável e imagem em `/media/…`; após aprovação,
  cada item com `script`, 5/5 artifacts de vídeo tocáveis e `assembled` renderável; `run_end`
  + `stream_end` limpos.

## Diagnóstico de erro HTTP 400 no GPT Image via Vercel (2026-06-30)

- Sintoma: `HTTPStatusError: 400 Bad Request` em `openai_image.generate_face`, sem
  corpo da resposta no traceback do LangGraph.
  - Causa: `httpx.Response.raise_for_status()` preservava o tipo da exceção, mas a
    mensagem não incluía o corpo JSON do gateway, onde vem o motivo real do 400.
  - Correção: `OpenAIImageAdapter` agora levanta `HTTPStatusError` verbose com
    `status`, `url` e `resp.text[:2000]`, mantendo log/metadata existentes.
  - Regressão: `test_openai_image_http_error_includes_response_body`.
- Sintoma: o corpo real do gateway retornou `safety_violations=[sexual]` e
  `isRetryable=false` para `openai/gpt-image-2`.
  - Causa: `creator_prompt` customizado substituía integralmente o prompt de imagem,
    sem guardrails fixas de retrato comercial adulto/vestido/não sexual.
  - Correção: prompts customizados agora entram como briefing dentro de um prompt base
    seguro (`adult professional UGC creator`, `modest everyday clothing`,
    `head-and-shoulders portrait`, `conservative commercial profile portrait`).
  - Regressão: `test_openai_image_wraps_custom_prompt_with_safety_guardrails`.
- Sintoma: mesmo com guardrails, a API continuou retornando
  `safety_violations=[sexual]`.
  - Causa: a própria guardrail negativa continha termos sensíveis explícitos
    (`sexual`, `nudity`, `lingerie`, `swimwear`, `erotic`), que podem acionar o
    classificador de imagem pelo texto do prompt.
  - Correção: prompt base reescrito como instrução positiva de retrato comercial
    conservador, sem lista negativa com vocabulário sensível explícito.
  - Regressão: `test_openai_image_safe_prompt_avoids_explicit_sensitive_terms`.

## Fase dashboard human-on-the-loop (D22) (2026-06-30)

Objetivo: transformar o dashboard de status em timeline operacional por item, sem novos
interrupts além do aceite humano de creators.

### Red → Green (TDD)

- RED: `tests/test_web_item_updates.py` expôs ausência de `_normalize_artifact`,
  `_normalize_creator`, `_build_item_update` e handler SSE `item_update`.
- GREEN:
  - `web/server.py`: contrato `item_update` a partir de `node_end` dos stages per-item,
    snapshots por item acumulados no run, normalização de artifacts (`kind`, `uri`,
    `media_type`, `renderable`) e creators (`image_uri`, `voice_ref`,
    `voice_preview_uri` + aliases `image`/`voice`).
  - `web/static/index.html`: timeline por item com conceito, script, mídia, QC e final;
    mídia só vira preview/player quando `renderable=true`; refs técnicas aparecem como
    texto rastreável.
- RED: `tests/test_creator_store.py` expôs que stores novos não persistiam os campos
  normalizados e stores antigos não preenchiam aliases novos no load.
- GREEN:
  - `creator_store.py`: grava campos normalizados de creator e carrega stores antigos sem
    erro, preenchendo `image_uri`, `voice_ref` e `voice_preview_uri`.
- Segurança UI: `renderItem` passou a montar DOM com criação de elementos e
  `textContent` para conceito/script, sem interpolar conteúdo gerado em template
  `innerHTML`.
- Verificação final: `rtk proxy python -m pytest` → **230 passed, 2 skipped, 2 warnings**.

### Falhas investigadas nesta fase

- Sintoma: import de `tests/test_web_item_updates.py` falhava na coleta por helpers
  inexistentes.
  - Causa: teste importava nomes ainda ausentes diretamente do módulo.
  - Correção: importar o módulo e deixar a ausência aparecer como falha executada por
    `AttributeError`, preservando o ciclo RED.
- Sintoma: teste estático da UI ainda falhava após trocar `renderItem` para helper DOM.
  - Causa: o helper `el()` usava `textContent`, mas o teste exigia evidência direta no
    corpo de `renderItem`.
  - Correção: tornar explícitas as atribuições `textContent` do id/hook/script dentro de
    `renderItem`.

## Fase de tracing coverage LangSmith (2026-06-30)

Objetivo: spans LangSmith em todas as etapas da pipeline, sem quebrar o modo
offline/mock.

### Red → Green (TDD)

- RED: `tests/test_tracing.py` falhava no Python 3.12 por `asyncio.get_event_loop()`;
  novos testes também expuseram ausência de `is_tracing_enabled`,
  `_drop_sensitive_inputs`, marcadores `__trace_*` e gate runtime.
- GREEN:
  - `tracing.py`: gate runtime por `LANGSMITH_TRACING`, sanitizer de inputs/outputs/
    metadata sensíveis, wrapper lazy para `@traced`, `wrap_anthropic_client` respeitando
    tracing off.
  - `tests/test_tracing.py`: async tests compatíveis com Python 3.12 + cobertura do
    sanitizer/gate/marcadores.
- RED: `tests/test_tracing_coverage.py` expôs ausência de spans em nodes/adapters e
  falta de `wrap_anthropic_client` no `AnthropicLLMAdapter`.
- GREEN:
  - Nodes em `nodes/stages.py` decorados com `@traced` e metadata leve por etapa.
  - `graph/builder.py`: `make_process_item_node`, spans em `process_item`, `fan_out`,
    roteamento de script e roteamento de QC.
  - `registry.py`: `CompositeAdapter` decorado por papel.
  - Adapters mock/reais/sub-adapters decorados; Anthropic client passa por
    `wrap_anthropic_client`.
  - `web/server.py`: caminho web também mescla `run_trace_config` no cfg do grafo.
- Correção de teste: `tests/test_cli.py` agora usa config temporário mock e força
  `LANGSMITH_TRACING=false` nos smoke tests, para não depender do `config/providers.yaml`
  live nem abrir trace real.
- Revisão xhigh pós-implementação apontou risco de vazamento de prompts/blobs e lacunas
  de cobertura. Correções aplicadas:
  - `tracing.py`: redaction recursiva de prompts/scripts/concepts/URLs/data URIs/base64;
    `offer` no root trace vira `offer_hash`.
  - `tests/conftest.py`: suíte força `LANGSMITH_TRACING=false` por padrão; tracing live
    precisa optar explicitamente.
  - `graph/builder.py`: factories testáveis para `fan_out`, `script.route` e `qc.route`.
  - Metadata do adapter de vídeo usa `step="video"` para cobrir Step 4 e Step 5; o node
    `product_demo` mantém `step=5`.
- Verificação final: `rtk proxy python -m pytest` → **219 passed, 2 skipped, 2 warnings**.

## Fase de system prompts + aceite humano + creator store + scope eval (2026-06-30)

Plano: `ticklish-crafting-tiger.md` (seções A–G).

### Red → Green (TDD)

**A — system_prompt kwargs (retrocompatível)**
- RED: `test_system_prompt.py` (12 testes) → falha em `build_creator(0, system_prompt=...)`
- GREEN:
  - `adapters/base.py`: `CreatorPort.build_creator` e `VideoPort.generate_clip` recebem `system_prompt=None`
  - `adapters/mock.py`: sufixo hash sha256[:8] nas URIs quando `system_prompt` presente; `None` = comportamento legado
  - `adapters/creator_real.py`: repassa `system_prompt` a `image.generate_face`
  - `adapters/openai_image.py`: usa `system_prompt` como `body["prompt"]` quando presente
  - `adapters/replicate_video.py`: adiciona `"prompt": system_prompt` em `body["input"]` quando presente
  - `nodes/stages.py`: `node_roster` lê `run_cfg.get("creator_prompt")`; `make_gen_node`/`node_product_demo` leem `run_cfg.get("video_prompt")`
  - `tests/test_resume_partial.py`: `FlakyAdapter.generate_clip` atualizado para aceitar `system_prompt=None`

**B — node_approval (gate humano via interrupt)**
- RED: `test_approval_gate.py` (6 testes) → `node_approval` não existia
- GREEN:
  - `nodes/stages.py`: novo `node_approval` usando `from langgraph.types import interrupt`; passthrough quando `approve_creators` falsy
  - `graph/builder.py`: wire `roster → approval → concepts`
  - Correção de lógica: `[]` (lista vazia de aprovados) = rejeitar todos (não default para todos)

**C — creator_store.py**
- RED: `test_creator_store.py` (11 testes) → `ModuleNotFoundError: orchestrator.creator_store`
- GREEN:
  - `creator_store.py` (novo): `record_creators`/`load_creators` espelhando `feedback_store.py`
  - `config.py`: `default_creator_store_path()` lê `ORCH_CREATORS`

**D — server.py (loop ciente de interrupt + endpoints)**
- GREEN:
  - `RunRequest`: campos `creator_prompt`, `video_prompt`
  - `_execute_run`: loop `while True` com `astream_events` + `aget_state` + interrupt handling; `record_creators` persiste
  - `POST /api/approve/{run_id}`: resolve Future da pipeline
  - `GET /api/creators`: retorna histórico do creator store
  - `PIPELINE_NODES`/`NODE_LABELS`: inclui `"approval"`

**E — dashboard (index.html)**
- GREEN:
  - 2 `<textarea>` para creator/video prompts
  - Painel de aceite (checkbox por creator + botão "Confirmar aceite") ao receber `awaiting_approval`
  - Botão "Histórico" no header → modal GET /api/creators com galeria de creators

**G — scope eval (LLM Judge)**
- RED: `test_scope_eval.py` (10 testes) → `scope_adherence_evaluator` e `SCOPE_CRITERIA` não existiam
- GREEN:
  - `adapters/judge.py`: `SCOPE_CRITERIA`, `scope_adherence_evaluator`, `evaluate_judge` generalizado (retrocompatível com `criteria=None, evaluator=None`)
  - `tests/cassettes/scope_eval.json`: golden com 3 pass + 2 fail
  - `tests/test_scope_eval.py`: replay determinístico + accuracy=1.0

### Probe offline (confirmação)

Atributo do interrupt no LangGraph 1.2.6 confirmado:
- `snap.tasks[0].interrupts[0].value` ✓ (via `PregelTask.interrupts`)
- `snap.interrupts[0].value` ✓ (via `StateSnapshot.interrupts` — campo direto)
- Ambos retornam o mesmo objeto `Interrupt(value={...})`
- `creators.json` escrito com status correto (approved/rejected)
- Roster filtrado corretamente após resume com subset aprovado

## Checklist de módulos (ordem TDD)

- [x] Scaffold (pyproject, uv venv, deps, configs) — `pyproject.toml`, `config/*.yaml`
- [x] `graph/state.py` — Item/BatchState/QCResult/JudgeVerdict + reducers (`test_state.py`)
- [x] `adapters/base.py` + `adapters/mock.py` — mocks determinísticos, custo por tier (`test_adapters_mock.py`)
- [x] `graph/routing.py` — tier routing + QC gate/loop (`test_routing.py`)
- [x] `nodes/stages.py` + `nodes/base.py` — stages da pipeline como nodes
- [x] `registry.py` — provider→adapter (mock + replicate)
- [x] `graph/builder.py` — StateGraph (subgrafo per-item + fan-out via Send) (`test_builder.py`)
- [x] `graph/checkpoint.py` — SQLite async-compatible saver (`test_checkpoint.py`)
- [x] `runner.py` + `cli.py` — run/status/resume/list + relatório (`test_graph_e2e.py`, `test_cli.py`)
- [x] `adapters/judge.py` — gateway config-driven + cassette/replay + eval (`test_judge_eval.py`)
- [x] **Fase de subagentes (Opus coordena, Sonnet executa):**
  - [x] **A** `feedback_store.py` (Step 10) + `test_feedback_store.py` (13)
  - [x] **B** `adapters/replicate_video.py` (VideoPort, httpx async injetável) + `test_replicate_video.py` (11)
  - [x] **C** `tests/test_resume_partial.py` — resume parcial validado (ver falha #5)
- [x] **Loop de feedback fechado** — `runner`/`cli` com `--feedback-store`; `prior_winning_styles`
      injetado no ciclo seguinte; viés na geração de conceitos (`mock.generate_concepts(bias=...)`,
      `LLMPort.generate_concepts` atualizado). Testes: `test_feedback_loop.py` (2), `test_concept_bias.py` (4).
- [x] Docs — `CLAUDE.md`, `docs/DECISIONS.md`, este arquivo, `README.md`

## MVP — Vercel AI Gateway (D20) — ✅ CONCLUÍDO

Decisão: usar o Vercel AI Gateway como ponto único para Claude e GPT Image 2.
Suíte: **132 passed, 1 skipped**. Nenhum teste mudou nestas tasks.

- [x] **Task 1** `adapters/openai_image.py` — `build_openai_image_vercel_adapter`
      (aponta para `https://ai-gateway.vercel.sh/openai/v1`, usa `AI_GATEWAY_API_KEY`)
- [x] **Task 2** `adapters/creator_real.py` — `build_real_creator_vercel_adapter`
      (OpenAI via gateway + Topaz direto + ElevenLabs direto)
- [x] **Task 3** `registry.py` — registrado `"creator_real_vercel"`
- [x] **Task 4** `config/providers.yaml` — `llm: vercel_gateway_llm`, `creator: creator_real_vercel`,
      `video: replicate`
- [x] **Task 5** `config/judge.yaml` — header Authorization aceita `AI_GATEWAY_API_KEY`
- [x] **Task 6** `.env.example` — `TOPAZ_API_KEY` e `ELEVENLABS_API_KEY` marcados `[LIVE]`
      no caminho direto/legado `creator_real_vercel`.

**Env vars para o perfil live atual (`creator_real_replicate`):** `AI_GATEWAY_API_KEY`,
`REPLICATE_API_TOKEN`, `REPLICATE_ELEVENLABS_MODEL` e, conforme o modelo hospedado,
os campos `REPLICATE_ELEVENLABS_*`. Tabelas em **D20/D24**.

**Smoke test pós-implementação:**
```bash
# CI (sem chaves — deve passar 100%)
rtk proxy python -m pytest

# Instancia os adapters reais do config/ atual
AI_GATEWAY_API_KEY=<chave> REPLICATE_API_TOKEN=<chave> REPLICATE_ELEVENLABS_MODEL=<owner/model:version> \
python -c "
from orchestrator.config import load_pipeline, load_providers
from orchestrator.registry import build_adapter_from_providers
p = load_pipeline(); prov = load_providers()
a = build_adapter_from_providers(prov, p)
print(type(a._by_role['llm']).__name__)      # AnthropicLLMAdapter
print(type(a._by_role['creator']).__name__)  # RealCreatorAdapter
print(type(a._by_role['video']).__name__)    # ReplicateVideoAdapter
"

# Run ponta a ponta
orchestrator run --batch 2 --offer "test product" --platform tiktok
```

## Próximos passos (v2, pós-MVP)

1. **Adapters reais** — *ligações criadas* (ver D17/D18); falta só chave no ambiente + flip:
   - [x] LLM via Vercel AI Gateway (`adapters/anthropic_llm.py`) — `llm: vercel_gateway_llm`
         + `AI_GATEWAY_API_KEY` ou `VERCEL_OIDC_TOKEN`.
   - [x] LLM direto Anthropic (`adapters/anthropic_llm.py`) — backward-compatible/legado;
         não é o caminho live recomendado do projeto.
   - [x] Creator live atual: GPT Image 2 + Replicate upscale + ElevenLabs via Replicate
         (`creator: creator_real_replicate`) + `AI_GATEWAY_API_KEY`/
         `REPLICATE_API_TOKEN`/`REPLICATE_ELEVENLABS_MODEL`.
   - [x] Creator direto/legado: GPT Image 2 + Topaz + ElevenLabs direto
         (`creator: creator_real` ou `creator_real_vercel`) + respectivas chaves diretas.
   - [x] Vídeo Replicate (`adapters/replicate_video.py`, D14) — `video: replicate` + `REPLICATE_API_TOKEN`.
   - **Pendente p/ rodar real:** (a) expor as chaves/envs no ambiente; (b) configurar o
     ref real `REPLICATE_ELEVENLABS_MODEL` e o schema `REPLICATE_ELEVENLABS_*` do modelo;
     (c) Step 8 segue mock (sem API única). Ver D24.
2. **Topologia data-driven**: mover nodes/edges para o `pipeline.yaml` (hoje fixa no builder).
3. **LangSmith**: setar `LANGSMITH_TRACING=true`/`LANGSMITH_API_KEY` p/ tracing; opcional
   subir o eval do Judge via `langsmith.evaluate` (hoje o evaluator roda local/offline).
4. [x] **CLI do loop**: `runner.run_cycles` + comando `orchestrator loop --cycles N
   --feedback-store ...` roda N ciclos encadeados; cada ciclo lê o feedback do anterior
   (viés nos conceitos) e grava o seu. Testes: `test_run_cycles.py` (3),
   `test_cli.py::test_cli_loop_*` (2). Ver **D16**.

## Falhas de teste investigadas (sintoma → causa raiz → correção)

1. **`process_item() missing 1 required positional argument: 'config'`**
   - Causa: o LangGraph só injeta `config` quando o parâmetro é tipado como
     `RunnableConfig`; estava `dict`.
   - Correção: anotar `config: RunnableConfig` no node (`graph/builder.py`).

2. **`SqliteSaver does not support async methods` (NotImplementedError)**
   - Causa: grafo roda via `ainvoke`, mas o checkpointer era o `SqliteSaver` sync.
   - Correção: usar `AsyncSqliteCompatSaver`, uma fachada async sobre `SqliteSaver`,
     porque `aiosqlite.connect` trava neste ambiente; ajustar os testes de checkpoint
     para a interface async (`aget_state`). (D9)

3. **`KeyError: '\n  "model"'` ao montar a request do Judge**
   - Causa: `str.format` interpretava as chaves literais do template JSON como campos.
   - Correção: substituir só os placeholders `{criteria_json}`/`{subject_json}` via
     `str.replace` (o template é JSON, não format-string).

4. **`Deserializing unregistered type ... Item` (warning, bloqueio futuro)**
   - Causa: pydantic Items no checkpoint sem tipo registrado no serializador.
   - Correção: `JsonPlusSerializer(allowed_msgpack_modules=[...Item/Artifact/QCResult])`. (D9)

5. **`RuntimeWarning: coroutine 'arun_with_retry' was never awaited`** (em `test_resume_partial.py`)
   - Sintoma: warning ao interromper um batch no meio (subagente C).
   - Causa: comportamento INTERNO do LangGraph — ao propagar a exceção, o executor
     (`pregel/_executor.py:181`) cancela as tasks pendentes do superstep do fan-out; as
     corrotinas pendentes são coletadas sem await.
   - Conclusão: **não é bug do produto**. Verificado que o resume parcial funciona correto
   no LangGraph 1.2.6 (checkpoint granular por task: itens concluídos não re-executam,
   pendentes sim; sem duplicar/perder). Warning é benigno; não foi suprimido para não
   mascarar comportamento real.

6. **`RuntimeError: There is no current event loop in thread 'MainThread'`** em
   `tests/test_tracing.py`
   - Causa: testes usavam `asyncio.get_event_loop()`; no Python 3.12 não há loop padrão
     garantido após execução de testes async.
   - Correção: migrar casos async para `pytest.mark.asyncio` e `await` direto.

7. **Smoke tests da CLI travavam em `test_cli_run_status_list`**
   - Causa: o teste usava `config/providers.yaml` do workspace, que está apontado para
     adapters reais; além disso, `.env` local pode ligar `LANGSMITH_TRACING=true`, abrindo
     tracing live durante teste offline.
   - Correção: criar `config-dir` temporário com providers mock e invocar a CLI com
     `LANGSMITH_TRACING=false`.

8. **Web indicava nenhum creator salvo e painel de streaming ficava sem output útil**
   - Sintoma: `/api/creators` retornava só `creators`, sem explicar qual store estava
     sendo lido; quando não havia tokens LLM, o painel "Output LLM (streaming)" seguia
     em "Aguardando LLM..." mesmo com eventos SSE de run/node/creator acontecendo.
   - Causa: o histórico depende do JSON em `ORCH_CREATORS`/`.orchestrator/creators.json`
     e a UI não mostrava esse caminho; o painel de stream só renderizava `llm_token`,
     ignorando eventos não-LLM como `run_start`, `node_start`, `creator_ready`,
     `awaiting_approval` e `item_update`.
   - Correção: `/api/creators` agora retorna `store_path` e `exists`; `node_roster`
     emite `creator_start` antes de cada geração; a UI registra progresso não-LLM no
     painel de streaming, mantendo tokens LLM quando existirem.

9. **Custo do LLM nunca aparecia no LangSmith, e tokens dependiam de dois caminhos
   duplicados (run externa `@traced` + run-filha `wrap_anthropic`)**
   - Sintoma: runs `llm` no LangSmith sem `total_cost`, e por vezes DUAS runs `llm`
     aninhadas para uma única chamada (a de fora, do `@traced`, sem tokens).
   - Causa raiz: `AnthropicLLMAdapter.__init__` envolvia o client com
     `wrap_anthropic_client` (langsmith `wrappers.wrap_anthropic`), que cria uma run-filha
     "ChatAnthropic" e tenta anexar `usage_metadata`/custo usando o price-map SERVER-SIDE
     do LangSmith. Esse price-map não reconhece `claude-opus-4-8` (modelo novo) nem
     `anthropic/claude-opus-4.8` (alias do Vercel AI Gateway, com prefixo de provider e
     ponto em vez de traço) — logo custo ficava ausente/zero, e a run-filha duplicava a
     contagem de tokens em paralelo à run externa do método decorado.
   - Correção: `src/orchestrator/tracing.py` ganhou uma tabela de preços local
     (`_LLM_PRICES_PER_MTOK`, USD/1M tokens) e as funções puras `_normalize_model`
     (normaliza alias de gateway/ponto para traço), `compute_llm_cost`,
     `build_usage_metadata` (tokens + custo, aditivo de cache) e `record_llm_usage`
     (anexa `usage_metadata`/`ls_model_name` na run atual via `get_current_run_tree()`,
     no-op seguro offline/sem tracing). `AnthropicLLMAdapter` parou de envolver o client
     com `wrap_anthropic_client` (fonte única = chamada manual de `record_llm_usage(
     response.usage, self.model)` logo após obter a resposta, nos dois ramos streaming/
     `create`, em `generate_concepts` e `write_script`) — elimina a run-filha duplicada e
     o custo passa a ser calculado localmente, independente do price-map do LangSmith.
   - Testes: `tests/test_llm_usage_cost.py` (13 casos, tracing.py + integração com o
     adapter). Ajustado `tests/test_tracing_coverage.py::test_anthropic_client_is_used_
     directly_without_wrapping` (antes `..._is_passed_through_tracing_wrapper`, que
     asserava explicitamente o wrapping — comportamento intencionalmente removido).
     Ajustado `tests/test_anthropic_llm.py::_make_response` para incluir `usage` (o
     fake de resposta não tinha esse campo; toda resposta real do SDK tem).

10. **`test_openai_image_wraps_custom_prompt_with_safety_guardrails` falhando**
    - Sintoma: `assert "modest everyday clothing" in prompt` (e depois
      `head-and-shoulders portrait` / `brand-safe product review context`) falhavam.
    - Causa raiz: edição manual em andamento no `_SAFE_CREATOR_PROMPT` (openai_image.py)
      tinha (a) removido as frases de guardrail "modest everyday clothing" e
      "head-and-shoulders portrait", (b) quebrado "brand-safe product review context"
      ao intercalar a frase dos olhos entre "product" e "review context", e (c) colado
      strings sem espaço (`(camera-ready).marketing`, `over-styling.portrait`).
    - Correção: `_SAFE_CREATOR_PROMPT` reescrito de forma coerente — restauradas as frases
      de segurança exigidas (todas como substrings contíguas e em minúsculas onde o teste
      espera) e corrigidos os espaços, **preservando** as adições de realismo do usuário
      (textura de pele/poros/imperfeições, "no over-styling", olhos engajados). A asserção
    do teste NÃO foi afrouxada — os guardrails são o comportamento desejado.
    - Suíte: **316 passando, 2 skips**.

11. **Roteamento de retry ainda escalava talking-head para `kling`/`seedance`**
    - Sintoma: após reprovação no QC, `select_tier(1, ["ltx", "kling", "seedance"])`
      retornava `kling` e `route_after_qc` enviava a próxima geração para o tier premium.
    - Causa raiz: a regra antiga usava `attempts` como índice do tier; o comportamento
      desejado agora é manter todas as tentativas em LTX e usar `attempts` apenas como
      orçamento do loop de QC.
    - Correção: `select_tier` passou a retornar sempre o primeiro tier configurado
      (`ltx` no config atual); testes de roteamento foram atualizados para a nova regra
      e `tests/test_builder.py` ganhou cobertura garantindo que itens regenerados acumulam
      somente clips `ltx`.

12. **Dashboard pausava pedindo aprovação de creators**
    - Sintoma: o dashboard entrava no `GraphInterrupt(type="approve_creators")` e ficava
      aguardando aceite/reprovação manual; quando o painel visual não aparecia, a execução
      parecia travada.
    - Causa raiz: `_execute_run` hardcodava `run.approve_creators=True` para todo run web,
      optando pelo gate humano mesmo quando o fluxo desejado era geração direta.
    - Correção: runs do dashboard agora usam `approve_creators=False`; o node de approval
      continua disponível para testes e invocações diretas que optem explicitamente pelo
      gate humano. Regressão coberta por
      `test_dashboard_run_bypasses_creator_approval_by_default`.

13. **Voz dos creators inaudível na web (Replicate ElevenLabs 422)**
    - Sintoma: na pipeline live (`creator_real_replicate`), imagem/upscale OK, mas a voz
      falhava com `POST .../elevenlabs/turbo-v2.5/predictions 422 — input: prompt is
      required`; `voice_id` virava `""`, `_build_voice_preview` devolvia `None` e a UI
      mostrava "sem voz" — nenhum áudio audível.
    - Causa raiz: `.env` fixava `REPLICATE_ELEVENLABS_MODEL=elevenlabs/turbo-v2.5` mas não
      o campo de texto; o `ReplicateVoiceAdapter` usava o default `text`, enquanto o modelo
      exige `prompt`. Confirmado ao vivo: campo de texto = `prompt`, campo de voz = `voice`,
      aceita nomes premade (ex.: `Rachel`), retorna `.mp3`.
    - Correção: `.env`/`.env.example` ajustados (`TEXT_FIELD=prompt`, `VOICE_FIELD=voice`).
      Adicionalmente, para não repetir voz entre creators do mesmo gênero, o adapter passou
      a ler cada `VOICE_ID_{FEMALE,MALE,NEUTRAL}` como **pool** (lista CSV) e escolher
      `pool[index % len(pool)]` — determinístico, casado com o gênero do `voice_profile`
      (que já alimenta a imagem). Regressões:
      `test_turbo_v25_sends_script_under_prompt` e
      `test_voice_pool_no_repeat_across_creators` em `tests/test_replicate_voice.py`.
      Suíte offline verde (2 skips `--live`).

14. **429 Too Many Requests derrubava upscale/voz/vídeo na pipeline live**
    - Sintoma: com conta Replicate de crédito baixo (<US$5, cap ~6 req/min, burst 1),
      o roster disparava N creators em paralelo (upscale + voz cada) e quase todas as
      chamadas voltavam `429 Request was throttled`; a voz (best-effort) virava `""`
      silenciosamente e o upscale caía no fallback da imagem original.
    - Causa raiz: nenhum rate limiting no cliente — o fan-out do grafo estourava o
      burst da conta instantaneamente; o retry usava só backoff exponencial curto,
      ignorando o hint "resets in ~Ns" do corpo do 429.
    - Correção: novo `adapters/_throttle.py` com `AsyncThrottle` (semáforo com
      `REPLICATE_MAX_CONCURRENCY`, default 1, + intervalo mínimo entre inícios
      `REPLICATE_MIN_INTERVAL_SECONDS`, default 10s) como singleton de processo
      compartilhado por voz, upscale e vídeo (`get_replicate_throttle()`, wired nas
      fábricas `build_real_creator_replicate_adapter` e `registry._build_replicate`).
      `with_transport_retry` agora extrai o hint de reset do 429 ("resets in ~8s" /
      "Expected available in 3 seconds") e espera `max(backoff, hint + 1s)`. Clock e
      sleep injetáveis — testes determinísticos, sem dormir.
      Regressões: `tests/test_replicate_throttle.py` e novos casos em `tests/test_retry.py`.

15. **Dashboard não deixava escolher a pessoa gerada para os vídeos**
    - Sintoma: o painel de aprovação de creators existia na UI mas nunca aparecia; os
      vídeos saíam com todos os creators gerados, sem escolha humana.
    - Causa raiz: `_execute_run` hardcodava `approve_creators=False` (decisão do item 12,
      que resolveu o "travamento" da época removendo o gate em vez de torná-lo opcional).
    - Correção: `approve_creators` virou campo do `RunRequest` (default `True`) propagado
      ao run config; a UI ganhou o checkbox "Escolher creators antes de gerar os vídeos"
      (ligado por padrão) no form. O run pausa no gate, mostra imagem+voz de cada creator
      e retoma só com os aprovados (o fan-out já atribuía `creator_ref`/`creator_image_uri`
      a partir do roster filtrado). Regressões:
      `test_dashboard_run_pauses_for_creator_approval_by_default` e
      `test_dashboard_run_can_bypass_creator_approval`.

16. **Reroll de voz era fake e o histórico mostrava creators sem mídia**
    - Sintoma: o botão "↻ Reroll" do painel de aprovação só trocava um bip sintético
      gerado no browser (nunca chamava o servidor); no caminho live, mesmo o endpoint
      `/reroll-voice` apenas renomeava a ref (`::reroll-N`) sem gerar voz nova. A galeria
      de creators listava entradas "só inspiração" (prompt sem imagem/voz).
    - Causa raiz: `RealCreatorAdapter` não implementava o contrato `reroll_creator_voice`
      (só o fallback genérico do stage rodava); o `CompositeAdapter` não delegava os ports
      opcionais do papel creator; `/api/creators` não filtrava entradas incompletas.
    - Correção: `RealCreatorAdapter.reroll_creator_voice` pede `create_voice(index +
      reroll_count)` — avança para a PRÓXIMA voz do pool do mesmo gênero (imagem e preset
      preservados); `CompositeAdapter.__getattr__` delega `reroll_creator_voice`/`voice`
      ao adapter do papel creator quando existem; o stage persiste a voz nova baixável em
      `voice-r{N}.{ext}` (path versionado — sem cache do áudio antigo na UI) e o botão da
      UI agora chama `rerollApprovalCreatorVoice` (endpoint real). `/api/creators` e a
      recuperação via media dir só retornam pessoas completas (imagem renderizável + voz
      tocável). Regressões: novos casos em `tests/test_creator_real.py`,
      `tests/test_registry_composite.py`, `tests/test_stages_reroll.py` e
      `tests/test_web_item_updates.py`. Suíte completa verde (358 passed, 2 skips `--live`).

---

## Nova UI "Kinetic Command" (front/ React SPA) — substitui o dashboard dark

**O quê:** implementação da UI/UX do projeto Stitch `2394034031028131565` (design system
"Kinetic Command", tema claro) — 12 telas navegáveis, substituindo o `static/index.html`
dark de página única. Frontend em **Vite + React + TypeScript + Tailwind** numa árvore
própria em `front/` (fonte em `front/src/`), buildado para `front/dist/` e servido pelo
FastAPI. Decisões do usuário: todas as 12 telas, ligadas a dados reais onde há backend;
stack React; substituir a UI antiga.

**Telas e wiring:**
- Reais via API/SSE: Dashboard, Campaigns (lista), Campaign Detail (pipeline + gate de
  aprovação de creators com reroll de voz), Create Campaign (wizard → `POST /api/run`),
  Concepts & Scripts, Creators Library (`/api/creators`), Job Queue e Video Review & QC
  (ambos via `/api/stream/{run_id}`), Integrations (`GET /api/integrations`, novo).
- Fiéis ao design com dados parciais/estáticos: Analytics (agrega `/api/status`),
  Settings (paths reais de stores), Publishing Calendar (fora de escopo — distribuição).

**Backend (`src/orchestrator/web/server.py`):** `GET /` serve `front/dist/index.html`
(fallback HTML instruindo `npm run build` quando não buildado — mantém CI sem Node verde);
mount `/assets` (check_dir=False) para os bundles do Vite; catch-all `GET /{path}` serve o
index para rotas client-side **sem** sombrear `/api|/media|/videos|/assets` (esses seguem
com 404/JSON). Novo `GET /api/integrations` lê `providers.yaml` (mapa stage→adapter). O
antigo `static/index.html` foi removido.

**Testes:** novo `tests/test_web_spa.py` (fallback, serviço do index buildado, catch-all
não-sombreando, `_front_index` em ambos os ramos, integrations). Suíte completa verde
(**537 passed, 2 skips `--live`, cobertura 100%**). Frontend: `tsc --noEmit` + `vite build`
sem erros.

- Testes obsoletos removidos (integridade): as asserções que faziam *grep* no HTML/JS do
  dashboard antigo (`test_ui_*` em `tests/test_web_item_updates.py` e `tests/test_web_prompts.py`)
  testavam o artefato deletado. Como a UI foi substituída por decisão do usuário, esses
  testes cobriam código removido — foram apagados; os comportamentos reais equivalentes
  (texto DOM-safe, reroll no servidor, preview de voz, prompt builder) vivem agora nos
  componentes React (cobertos por `tsc` + build). Os testes de **lógica de backend**
  (`_build_item_update`, normalizadores, `/api/prompts` CRUD etc.) foram mantidos intactos.

**Como buildar/rodar:** `cd front && npm install && npm run build` → `orchestrator serve`
(dashboard em `http://localhost:8000/`). Dev: `cd front && npm run dev` (Vite faz proxy de
`/api`,`/media`,`/videos` para :8000).

---

## Gate de edição de Concepts & Scripts antes do creator

**O quê:** a pipeline agora gera `concepts` e `scripts` antes do roster de creators e
pausa em um gate humano opcional para editar campos do conceito, editar o script e
descartar conceitos antes de gastar creator/vídeo.

**Backend/grafo:** `graph/builder.py` foi reordenado para
`concepts -> scripts -> concept_review -> roster -> approval -> fan-out`. O subgrafo
per-item não tem mais node `script`; ele entra direto no roteamento de tier. O fan-out
move `concept["script"]` para `Item.script` e remove a chave do concept. `stages.py`
ganhou `node_scripts` batch-level e `node_concept_review` com passthrough quando
`run.edit_concepts` é falso.

**Web/UI:** `RunRequest.edit_concepts` default `True`; `_execute_run` emite
`awaiting_concept_edit` e retoma via `POST /api/approve/{run_id}/concepts`. O front
tipa `EditableConcept`, adiciona fase `editing`, guarda `editConcepts` na stream e a tela
Concepts & Scripts renderiza editor com textareas por campo, textarea grande para
`script`, checkbox de inclusão/exclusão e submit para continuar.

**Falha investigada no smoke dos dois gates:**
- Sintoma: com `edit_concepts=True` e `approve_creators=True`, o item era processado e o
  script editado chegava aos `item_update`, mas o evento `run_end.summary` vinha com
  `produced=0`.
- Causa raiz: `_execute_run` montava o summary a partir do último evento `LangGraph`
  observado em `astream_events`; com subgrafo + interrupts, esse evento pode ser output
  intermediário/subgrafo, não o estado raiz final.
- Correção: ao sair do loop sem interrupt pendente, `_execute_run` agora lê
  `graph.aget_state(cfg).values` e usa esse snapshot raiz como `final_output`.
  Regressão: `test_dashboard_run_summary_after_concept_edit_and_creator_approval`.

**Testes/verificação:** suíte backend verde com `rtk proxy python -m pytest`
(**537 passed, 2 skips, cobertura 100%**). Frontend verde com `npm run build`
(`tsc --noEmit` + Vite build). Smoke in-process do fluxo web com `config-mock`: gate
`awaiting_concept_edit` antes de qualquer creator, gate `awaiting_approval`, `produced=1`
e script editado propagado. O smoke por porta TCP local foi bloqueado pelo sandbox de
socket entre sessões, então a verificação usou o app ASGI/funções de endpoint no mesmo
processo.

## Correção — scripts vazios no front + Draft Video inerte

**Sintoma:** `/scripts` podia abrir sem conceitos/scripts para runs existentes, e o botão
`Draft Video with <creator>` na galeria de creators não disparava nenhuma ação.

**Causa raiz:** o front dependia apenas de `/api/stream/{run_id}`. Esse stream vive em
memória (`_runs`) e não hidrata runs já checkpointados; para esses casos, `/api/status`
devolvia só o resumo agregado, sem itens/conceitos/scripts. Além disso, o botão
`Draft Video` era só visual: não tinha handler, não enviava o creator selecionado e o
backend não tinha caminho para reutilizar um creator existente como roster fixo.

**Correção:** novo `GET /api/state/{run_id}` combina checkpoint SQLite com estado runtime
do web server e devolve `items`, `edit_concepts`, `awaiting`, `phase` e `summary` para
hidratação da SPA. `useRunStream` carrega esse estado antes/ao lado do SSE, e `/scripts`
aceita `?run=<run_id>`. `RunRequest` agora aceita `creator_id`/`creator_run_id`; o backend
resolve o creator salvo ou recuperado de mídia, injeta `seed_creator` no run config, e
`node_roster` reutiliza esse creator sem chamar `build_creator`. A galeria chama
`POST /api/run` com o creator selecionado e navega para `/scripts?run=<novo_run_id>`.
`CampaignDetail` também mostra CTA direto para revisão quando o run está no gate
`editing`.

**Regressões:** `test_run_state_returns_checkpoint_items_with_scripts`,
`test_run_state_returns_pending_concepts_during_edit_gate`,
`test_node_roster_uses_seed_creator_without_building_new_creator` e
`test_execute_run_with_seed_creator_uses_selected_creator`. Verificação focada:
`rtk proxy python -m pytest --no-cov tests/test_web_endpoints.py tests/test_web_item_updates.py tests/test_stages_coverage.py tests/test_builder.py`
→ **87 passed, 1 warning**; frontend `rtk npm run build` → verde.

**Falha investigada pós-integração:** a suíte completa passou funcionalmente
(`541 passed, 2 skipped`), mas quebrou no gate de cobertura com total **99.04%**.
Os buracos eram ramos novos de fallback/erro: seed creator sem id,
`_find_creator_for_draft` via mídia recuperada/404 com `creator_run_id`,
fases runtime (`idle`/`running`/`awaiting`/`done`) e snapshots runtime em
`/api/state`. Correção: adicionar regressões específicas em
`tests/test_web_endpoints.py` e `tests/test_stages_coverage.py`, sem afrouxar
asserts nem o gate `fail_under=100`. Verificação focada:
`rtk proxy python -m pytest --no-cov tests/test_web_endpoints.py tests/test_stages_coverage.py`
→ **60 passed, 1 warning**.

**Ajustes pós-review:** o revisor encontrou três riscos reais na integração web.
`/api/creators` agora normaliza entradas do store antes de responder, garantindo
`id` público mesmo quando o JSON salvo só tem `creator_id` e preservando os
metadados do histórico. `useRunSelection` deixou de forçar `?run=` de volta a cada
seleção manual: o run preferido só é reaplicado quando o query param muda. O reducer
do SSE limpa `editing`/`awaiting` e volta para `running` no primeiro `node_start` ou
`item_update` após o gate, evitando formulário stale durante a geração. Verificação:
`rtk proxy python -m pytest --no-cov tests/test_web_item_updates.py tests/test_web_endpoints.py tests/test_stages_coverage.py tests/test_builder.py`
→ **95 passed, 1 warning**; `cd front && rtk npm run build` → verde; suíte final
`rtk proxy python -m pytest` → **549 passed, 2 skipped**, cobertura **100%**.

## Bugfix — assembly resiliente + itens órfãos na UI

**Sintoma:** um vídeo real do Replicate (`.../tmpuwbfz9mf.mp4`) não aparecia na UI. O
run `web-fc45f29e` ficou invisível por completo apesar de ter 2 clips reais no disco
(`.orchestrator/videos/web-fc45f29e/items/concept-0001/clip-{0,1}.mp4`) e QC aprovado.

**Causa (2 bugs):**
1. **Assembly sem resiliência.** `node_assembly` chamava `adapter.assemble` sem
   try/except; o assembler live (Seedance via Vercel Gateway) recusou a imagem
   ("input image may contain real person") e levantou `RuntimeError`. Exceção num node
   do subgrafo aborta `process_item.ainvoke` **antes** do write em `results`
   (`builder.py:135`), matando o item.
2. **Itens falhos somem da UI.** `runner.get_status`/`summarize` e o branch
   checkpoint-only de `/api/state` liam só o canal `results`. Sem o item lá → 0 itens,
   mesmo com os clips no disco. Os clips ficavam órfãos no estado do subgrafo per-item
   (checkpoint_ns `process_item:<task_id>`), nunca lido.

**Correção:**
1. `node_assembly` (`nodes/stages.py`) passou a envolver `assemble` em try/except +
   `_ensure_artifact` (valida shape antes de usar — regra
   `adapter-composition-must-validate-shape`). Falha → item completa sem `assembled`,
   com `Item.error` (novo campo em `graph/state.py`), preservando os clips → entra em
   `results`. Knob opt-in `assembly.allow_mock_fallback` (default off) degrada para um
   final mock com `fallback_reason` em vez de surfar o erro.
2. `runner.get_pending_items` recupera itens em voo/falhos direto do checkpoint via
   `aget_state(subgraphs=True)` (usa o hook `aget_tuple`/subgraph state antes inerte),
   com clips + erro da task limpo (`_clean_task_error`). `/api/state` faz merge desses
   órfãos com `results` (dedupe por id; results vence). `error` propagado em
   `_snapshot_from_item`/`_complete_item_payload` e no `types.ts`/`VideoReview`/
   `CampaignDetail` (badge "Assembly Failed" + motivo; clips continuam tocáveis).

**Verificação:** TDD (red→green) por bug; run real `web-fc45f29e` volta a aparecer no
`/api/state` com 2 artifacts de vídeo + erro **sem re-rodar**; `cd front && npm run
build` → verde; suíte `rtk proxy python -m pytest` → **560 passed, 2 skipped**,
cobertura **100%**.

## Mudança — upscale movido da imagem para o vídeo final

**Pedido:** "o upscale só no vídeo, não na imagem."

**Antes:** o upscale vivia dentro do creator (`RealCreatorAdapter.build_creator` chamava
`TopazUpscaleAdapter`/`ReplicateUpscaleAdapter` na face → `upscaled_base`). O vídeo nunca
era upscalado. Efeito colateral: uma face mais fotorrealista aumenta a chance da rejeição
"input image may contain real person" no gerador de vídeo (ver bugfix anterior).

**Depois:**
- **Creator não upscala a imagem.** `build_creator` usa a face crua como `upscaled_base`
  (nome mantido por compat). Fábricas (`build_real_creator_*`) deixam de construir o
  upscaler de imagem; `topaz` vira param opcional/ignorado só por compat de assinatura.
- **Novo papel `upscale` + `node_upscale`** rodam pós-montagem, uma vez, sobre o
  `assembled` (Step 8): `assembly → upscale → END` no subgrafo. Best-effort (montagem
  ausente/passthrough/erro → mantém o vídeo montado). Marca `meta.upscaled=True` e
  `meta.upscaled_from`.
- **Adapters:** `MockAdapter.upscale` (determinístico, config-mock) e novo
  `PassthroughUpscaleAdapter` (no-op, perfil live até plugar um upscaler de vídeo real).
  `UpscalePort` em `adapters/base.py` reusa a assinatura `upscale(url)->url` dos
  upscalers de imagem — um upscaler de vídeo real pluga trocando o nome em
  `providers.yaml`. Registry: `ROLES += "upscale"`, `CompositeAdapter.upscale`.
- **Config:** `config/providers.yaml → upscale: passthrough_upscale`;
  `config-mock → upscale: mock`.
- **UI:** node `upscale` entra em `PIPELINE_NODES`/`ITEM_UPDATE_NODES`, `NODE_LABELS`
  ("Upscale (vídeo)") e no grupo "Assembly" do `CampaignDetail`.

**Verificação:** TDD (node_upscale, mock.upscale, passthrough, roteamento do composite);
e2e mock (`test_final_video_is_upscaled_not_the_image`) confirma `assembled.meta.upscaled`
e a base do creator crua. Suíte `rtk proxy python -m pytest` → **568 passed, 2 skipped**,
cobertura **100%**; `cd front && npm run build` → verde.

## Falha de run inteiro agora visível na UI (fase "error" + lista "Failed")

**Sintoma:** quando a pipeline quebrava, a falha não era demonstrada na interface de forma
persistente. O erro só aparecia no evento SSE `error` e apenas se o usuário estivesse
assistindo o run ao vivo no `CampaignDetail`. Ao reconectar, navegar ou olhar a lista de
campanhas, a falha sumia.

**Causa (`src/orchestrator/web/server.py`):**
- `_execute_run` (`except`) emitia o evento SSE mas **não gravava** o erro; o `finally` só
  setava `state["done"]=True`.
- `_runtime_phase` retornava **"done"** para um run quebrado (done=True), então `/api/state`
  hidratava a fase como "done" e a falha desaparecia na reconexão.
- O `RunDetail` de `/api/state` não tinha campo `error` — a caixa de erro do `CampaignDetail`
  ficava vazia mesmo se a fase fosse "error".
- `/api/runs` reportava `active = list(_runs.keys())`; runs quebrados continuavam em `_runs`,
  logo apareciam como "Generating" para sempre na lista. `rowStatus` (Campaigns) só marcava
  "Failed" para `dropped>0 && approved===0`, nunca para um crash.

**Correção:**
- `_execute_run`: grava `state["error"] = str(exc)` no runtime além de emitir o SSE.
- `_runtime_phase`: retorna "error" quando `state["error"]` (checado antes de "done").
- `run_state` (`/api/state`): inclui `"error"` no payload.
- `list_runs_endpoint` (`/api/runs`): `active` = só o que está realmente rodando (sem `error`
  nem `done`); novo campo `errored`. De quebra, para de rotular runs concluídos como
  "Generating".
- Front: `RunsIndex.errored` e `RunDetail.error` em `types.ts`; `hydrate` propaga `error`;
  `Campaigns.rowStatus` marca "Failed" para runs em `errored`.

**Limitação conhecida:** o erro de run inteiro vive só no runtime in-session (`_runs`); um
restart do servidor o perde (o node quebra antes de escrever no checkpoint). Falhas **por
item** seguem persistidas via recuperação de órfãos (`runner.get_pending_items`).

**Verificação:** TDD — `test_runtime_phase_branches` (ramo error vence done),
`test_run_state_surfaces_run_crash_error`, `test_list_runs_endpoint_reports_errored_and_excludes_from_active`.
Suíte `rtk proxy python -m pytest` → **572 passed, 2 skipped**; `cd front && npm run build`
→ verde.

## Reutilização de creator com adapters reais gerava "outra pessoa"

**Sintoma:** ao reutilizar um creator específico (tela Creators → draft), com adapters
reais o vídeo saía com um creator **diferente** do escolhido.

**Causa:** a referência de imagem do creator reutilizado chegava ao provider como um
**path local** `/media/{run}/{creator}/image.png`, que o serviço externo (Replicate) não
consegue baixar → a referência era efetivamente perdida e o modelo de vídeo gerava outra
face. Cadeia: `persist_creator_media` reescreve `upscaled_base` para o path `/media/...`
e guarda a origem em `image_source_uri`; o store (`creator_store`) só persiste o path
local; na reutilização o seed carrega só esse path e o fan-out
(`builder.py`: `image_source_uri or upscaled_base`) o repassa cru ao adapter de vídeo.

**Correção:** reconstruir uma referência **buscável pelo provider** a partir do arquivo
local em disco, na reutilização.
- `media_store.data_uri_from_media_path(uri, media_root)` — novo helper: mapeia um path
  `/media/...` para o arquivo em disco e devolve um `data:` URI (durável, não expira);
  `None` para URIs remotas/data: (não precisam) ou arquivo inexistente.
- `nodes/stages._ensure_seed_reference_image` — no `node_roster` (caminho do seed), quando
  a referência do seed não é http(s)/`data:`, reconstrói `image_source_uri` a partir do
  arquivo local. No-op quando já é buscável (mantém data:/http do seed).

**Limitação conhecida:** depende do arquivo local ainda existir sob `media_root`. Se a
mídia foi limpa, a referência permanece o path `/media/...` e a geração falha — agora
**visível** na UI (ver bugfix de falha de run acima).

**Verificação:** TDD — `test_data_uri_from_media_path_*` (media_store),
`test_node_roster_seed_reconstructs_reference_from_local_media` e
`test_node_roster_seed_keeps_remote_reference_untouched` (stages). Suíte
`rtk proxy python -m pytest` → **576 passed, 2 skipped**, cobertura 100%.

## Transformação agent — Fase 0: ativação do modo agent (2026-07-15)

Objetivo: ligar o loop agentic (critique→refine) que já existia implementado mas estava
dormente. Toda a máquina (`AgentPort.run_stage_agent`, `stage_executor`, `agent_catalog`)
já estava pronta e testada; faltava apenas nenhuma config ativá-la — `config/agents.yaml`
declarava todos os stages como `executor: tool, agent_enabled: false`.

### Red → Green (TDD)
- RED: `test_live_config_activates_agent_mode_on_llm_stages` (test_live_config_no_mock.py)
  afirma que o perfil live (`config`) ships `concepts`/`scripts` em `executor: agent,
  agent_enabled: true` e mantém os stages de mídia em modo tool. Falhou (config ainda tool).
- GREEN: `config/agents.yaml` — `concepts` e `scripts` viram `executor: agent,
  agent_enabled: true`. Nenhum código de produto mudou; o roteamento agent já existia no
  `stage_executor`. `config-mock/agents.yaml` permanece tool (perfil offline/dry-run).

### Falha investigada (sintoma → causa → correção)
- **Sintoma:** `test_project_config_dirs_ship_valid_agents_yaml[config]` quebrou.
- **Causa:** o teste travava o estado *antigo* (concepts/scripts sempre `executor == "tool"`)
  para ambos os perfis. O comportamento desejado do perfil live mudou legitimamente na Fase 0.
- **Correção:** o teste passou a esperar `executor` específico por perfil — `agent` para
  `config`, `tool` para `config-mock` — mantendo as demais asserções (tools por stage,
  validade do YAML). Não foi afrouxamento: continua provando o contrato, agora correto.

**Escopo:** o loop ativado ainda é o wrapper bounded de 2 passos (draft→critique→refine ×1),
não um loop de tool-calling. A Fase 1 (tool-calling real) é a próxima etapa do roadmap.

**Verificação:** `rtk proxy python -m pytest` → suíte verde, cobertura 100%. Ao vivo:
`orchestrator run --batch 2 --offer "serum X" --config-dir config` com `AI_GATEWAY_API_KEY`
setado mostra `agent_backend`/`agent_revised` no trace do LangSmith.

## Transformação agent — Fase 1: loop de tool-calling real (2026-07-15)

Objetivo: substituir o wrapper agentic fixo de 2 passos (draft→critique→refine ×1) por um
**loop de tool-calling real** — o modelo recebe schemas das tools, escolhe quais chamar e
itera multi-pass até convergir ou estourar um budget. Ver ADR **D32**.

### Red → Green (TDD)
- `tools/registry.py`: `ToolSpec.parameters` (JSON schema agent-facing) + `tool_call_schemas`.
  concepts/scripts expõem só `revision`; media tools = schema vazio (Fase 2).
  Testes: `test_tool_registry_exposes_agent_parameter_schemas`,
  `test_tool_call_schemas_builds_neutral_schema_for_allowed_tools` (test_tools.py).
- `adapters/_agent_loop.py` (novo): loop compartilhado provider-agnostic + `ToolCall` +
  `AgentBrain` Protocol. Centraliza budget (`max_steps`), fronteira D29 (só `run_tool`),
  enforcement de `allowed_tools` e safety-net (garante ≥1 output de domínio válido).
  Testes: `tests/test_agent_loop.py` (single-call, multi-pass, budget, safety-net, allowlist).
- `stage_executor.py`: closure `run_tool(tool_name, **inputs)` — o agent nomeia a tool; o
  executor valida contra `allowed_tools` e mantém offer/n/seed server-authoritative
  (filtra args do modelo aos params declarados). Novo `_agent_max_steps` lê `agent.max_steps`
  do pipeline. Teste: `test_stage_executor_agent_run_tool_enforces_boundary_and_budget`.
- Adapters `mock.py` / `gateway_llm.py` / `anthropic_llm.py`: `run_stage_agent` reescrito
  sobre `run_agent_loop`, cada um com seu brain (`_MockAgentBrain` determinístico,
  `_GatewayAgentBrain` OpenAI function-calling via httpx, `_AnthropicAgentBrain` `tool_use`
  do SDK). `_agent_critique` (crítica-como-diretiva) removido — coberto pelo novo loop.
- `config/pipeline.yaml`: seção `agent.max_steps: 4` (budget documentado; default se ausente).

### Contratos alterados (comportamento desejado mudou — não afrouxamento)
- `StageToolRunner`: de `run_tool(**inputs)` para `run_tool(tool_name, **inputs)`. Os testes
  agentic de mock/gateway/anthropic foram reescritos para o novo contrato de tool-calling
  (draft inicial via tool nomeada; refino via 2ª chamada com `revision`; budget; safety-net;
  allowlist). A cobertura foi **substituída**, não reduzida: os testes de `_agent_critique`
  deram lugar a testes do loop real.

### Falhas investigadas (sintoma → causa → correção)
- **Cobertura 99.6%** após o rewrite: branches defensivos/futuros não exercitados —
  (a) resolução multi-tool no closure (Fase 2): **removida** por YAGNI (entra na Fase 2 com
  teste); (b) guard D29 do closure, knob `max_steps`, `_summarize_result` (ref. circular),
  resposta malformada do gateway e args de tool inválidos: cobertos com testes diretos.
  Voltou a 100%.

**Escopo mantido fora (Fase 2/3):** multi-tool por stage, agentificar mídia
(`_AGENT_STAGES` ainda = concepts/scripts), streaming de token, judge proxy, R2.

**Verificação:** `rtk proxy python -m pytest` → **687 passed, 2 skipped**, cobertura 100%.
O pipeline mock agentic ponta a ponta (`test_mock_pipeline_can_opt_into_agentic_concepts_and_scripts`)
exercita o novo loop através do grafo. Ao vivo: `orchestrator run --config-dir config` com
`AI_GATEWAY_API_KEY` mostra `agent_steps` no trace.

## Fase 2 (D33): stage `video` agentic (2026-07-16)

**Entregue:** o agent dirige a geração de clips. `_AGENT_STAGES` ganha `video`;
`generate_clip` expõe `revision` (diretiva apendada ao brief server-authored);
`run_agent_loop` devolve `AgentRunResult` (output final + todas as tentativas); erro de
tool vira feedback ao modelo; budget e cap de chamadas por stage.

**Arquivos:** `adapters/_agent_loop.py` (AgentRunResult/ToolAttempt, try/except no
run_tool, `summarize_tool_result`), `adapters/base.py` (DEFAULT_MAX_STEPS + AgentPort
atualizado), `stage_executor.py` (`with_attempts`, `_agent_max_steps(pipeline, stage)`,
`_agent_max_tool_calls`), `tools/video.py` (`_compose_prompt`), `tools/registry.py`
(`_VIDEO_REVISION_PARAM_SCHEMA`), `nodes/stages.py` (`_settle_takes`),
`agent_catalog.py`, `config/agents.yaml`, `config/pipeline.yaml`.

### Contratos alterados (comportamento desejado mudou — não afrouxamento)
- `run_agent_loop`/`run_stage_agent`: de `(result, executed)` para `AgentRunResult`.
  Dataclass, não tupla: a Fase 3 (tokens/latência) quebraria os call-sites de novo.
- `execute_stage_tool(..., with_attempts=False)`: sem o opt-in, o retorno mudaria de tipo
  entre modo tool e agent e quebraria concepts/scripts. Com `with_attempts=True` o retorno
  é `AgentRunResult` **também** em modo tool e no passthrough (tentativa sintética
  `id="direct"`), para o node de vídeo ter um só caminho de contabilidade.
- `test_live_config_no_mock` e `test_tools::test_tool_registry_exposes_agent_parameter_schemas`
  afirmavam "mídia fica em tool / schema vazio". Passaram a afirmar o novo comportamento.
  Dois testes usavam `video` como exemplo de stage **proibido** em modo agent
  (`test_agent_catalog`, `test_stage_executor`); o exemplo virou `roster`, que segue fora
  do gate — o invariante continua provado.

### Falhas investigadas (sintoma → causa → correção)
- **Premissa errada no plano — "fan-out paralelo por tier".** Sintoma: o plano previa
  escrita concorrente em `item.clips` e colisão de índice em `persist_item_media`. Causa:
  `builder.py:57` usa `add_conditional_edges(START, make_script_route_node(tns), ...)` —
  é um **router**, um só node de tier roda por item; o paralelismo é por item
  (`batch.max_concurrency`). Prova: `Item.clips` é `list[Artifact]` **sem reducer**
  (`graph/state.py:72`), então fan-out real já seria `InvalidUpdateError` hoje. Correção:
  desenho simplificado, sem tratamento de concorrência.
- **`RecursionError` no `summarize_tool_result`.** Sintoma:
  `test_summarize_tool_result_falls_back_on_unserializable` estourou a pilha em vez de
  cair no fallback. Causa: `_elide_data_uris` desce na estrutura, então uma referência
  circular estoura **antes** de o `json.dumps` virar `ValueError` (o único erro que o
  código antigo esperava). Correção: `except (TypeError, ValueError, RecursionError)`.
- **Cobertura 99,94%** após o refactor: o `except` do `model_dump()` era um branch
  defensivo especulativo (nenhum caso real). Correção: `model_dump()` foi para dentro do
  `try` existente — mais simples e coberto pelo mesmo teste, com um `model_dump` que
  levante caindo no fallback do `repr`. Voltou a 100%. (Mesmo critério da Fase 1: branch
  sem caso real sai, não ganha teste artificial.)
- **Bug latente corrigido:** a safety-net usava o sentinela `last_result is None`, que
  confundia "o modelo nunca chamou uma tool" com "a tool rodou e retornou `None`" — e
  disparava uma **segunda chamada paga** invisível. Agora há um flag `had_success`
  explícito. Coberto por `test_agent_loop_does_not_call_safety_net_when_a_tool_returned_none`.

**Escopo mantido fora (Fase 3):** `roster`/`assembly`/`upscale` agentic, multi-tool por
stage (segue YAGNI: nenhum stage tem 2 tools legítimas), streaming de token, judge proxy,
R2. Risco aceito: custo de take que falhe após a cobrança do provider não é contabilizado.

**Verificação:** `rtk proxy python -m pytest` → **737 passed, 2 skipped**, cobertura 100%
(era 687). `orchestrator run --batch 2 --offer "serum X" --config-dir config-mock` → 2
produzidos, 2 aprovados, custo mock $0.64. O caminho agentic de vídeo pelo grafo inteiro
é coberto por `test_mock_pipeline_can_opt_into_agentic_video` (offline, custo zero).
Ao vivo ainda não rodado: exige `AI_GATEWAY_API_KEY` + Replicate (custo real).

## Fase 3 (D34): streaming de tokens no GatewayLLMAdapter (2026-07-16)

**Entregue:** o adapter LLM default do perfil live passa a emitir tokens ao vivo para o
dashboard. `_chat(..., stage=...)` → SSE (`"stream": true`) → `llm_start`/`llm_token`/
`llm_end` no `stream_bus`. Contrato do front inalterado.

**Arquivos:** `adapters/gateway_llm.py` (`_sse_payload`, `_stream_chat`, `_consume_sse`,
param `stage` em `_chat`; call-sites `generate_concepts` → `"concepts"` e `write_script` →
`"script:<id>"`), `front/src/api/useRunStream.ts` (reset do buffer no `llm_start`).

### Decisões de desenho
- **`stage` é o gate do streaming.** O brain do agent chama `_chat` sem `stage`, então o
  loop agentic nunca streama — paridade com o Anthropic e sem remontar `tool_calls`
  fragmentados do SSE. Em modo agent quem streama é a chamada de domínio dentro do
  `run_tool`, que é o que o usuário quer ver.
- **Retry não reemite token por construção** (ver D34): `_is_retryable` só cobre erros
  pré-envio e 429. Nenhum código novo de guarda foi preciso.

### Falhas investigadas (sintoma → causa → correção)
- **UI concatenaria dois JSONs no painel de LLM.** Sintoma: dirigindo o loop agentic com
  streaming (script fora da suíte), o stage `concepts` emitiu **2** `llm_start` e o texto
  acumulado deu 246 chars para um payload de 123 — a revisão grudou no draft. Causa: em
  modo agent `generate_concepts` roda 2x (draft + revisão) e o reducer do front tratava
  `llm_start` só como "active: true", sem zerar o `text` do stage. Correção: `llm_start`
  zera o buffer daquele stage. Era pré-existente (o Anthropic tem a mesma forma), mas só
  ficou visível ao ligar streaming no adapter default. Verificado reproduzindo o reducer
  contra a sequência real de eventos: 246 → 123 chars.
- **Cobertura 99,94%:** o ramo "sem client injetado" do `_stream_chat` (produção cria o
  próprio `AsyncClient`) não era exercitado. Correção: espelhado o
  `test_uses_own_client_when_not_injected` já existente para o caminho de streaming.
  Voltou a 100%.

**Escopo mantido fora:** judge proxy ao vivo + wiring do `GatewayJudge` no QC, R2
(`R2MediaStorage`, D30), streaming das rodadas de decisão do agent.

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **746 passed, 2 skipped**,
cobertura 100%. `tsc --noEmit` do front limpo. O caminho agentic + streaming foi dirigido
fora da suíte (MockTransport servindo SSE) para observar os eventos reais — foi assim que
o bug do reducer apareceu. Ao vivo ainda não rodado: exige `AI_GATEWAY_API_KEY` (custo
real); em particular, **`stream_options.include_usage` só pode ser confirmado ao vivo** —
se o gateway ignorar o campo, o custo do run vai a zero (mesmo comportamento que o
caminho não-streaming já tem quando o `usage` vem ausente).

## Diagnóstico local Neon/RLS (2026-07-26)

### Falhas investigadas (sintoma → causa → correção)

- **`GET /api/runs` e `GET /api/creators` retornavam 500 com
  `papel runtime 'neondb_owner' não pode ter SUPERUSER/BYPASSRLS`.** Causa confirmada
  diretamente no Neon: a `DATABASE_URL` local usa `neondb_owner`, cujo atributo é
  `BYPASSRLS=true`; a role restrita `orchestrator_runtime` ainda não existe. A mesma URL
  usa o endpoint `-pooler`, embora o contrato operacional exija conexão direta.
  Correção operacional: reservar a conexão direta de `neondb_owner` para
  `MIGRATION_DATABASE_URL`, executar `migrate` e `db provision-runtime`, e configurar
  `DATABASE_URL` com a conexão direta da nova role `orchestrator_runtime`.
- **O gate focado de PostgreSQL terminou em dois erros de setup.** Sintoma:
  `test_cli_provisions_fixed_runtime_role_without_echoing_password` e
  `test_database_rejects_a_runtime_role_that_bypasses_rls` não chegaram às asserções.
  Causa: o fixture `pytest-postgresql` está configurado para o PostgreSQL local em
  `127.0.0.1:5432`, que não estava rodando (`Connection refused`). Nenhum teste ou
  asserção foi alterado. A reprodução direta contra o Neon executou duas vezes e produziu
  deterministicamente o erro esperado; a role remota também foi inspecionada como
  `rolsuper=false`, `rolbypassrls=true`.
- **`db provision-runtime` falhou após a migration com `permission denied to alter
  role`.** Causa: `provision_runtime_role()` agrupa `NOSUPERUSER`, `NOBYPASSRLS` e
  `NOREPLICATION` no `ALTER ROLE`; PostgreSQL exige `SUPERUSER` até para desligar esses
  atributos. O `neondb_owner` do Neon possui `CREATEROLE`, mas não `SUPERUSER`, portanto
  consegue iniciar a criação e não consegue executar esse hardening explícito. A
  transação sofreu rollback: `orchestrator_runtime` permaneceu ausente, enquanto Alembic
  chegou corretamente a `20260725_0008`. Correção aplicada no código: roles novas usam
  os defaults não privilegiados; roles existentes têm `rolsuper`/`rolbypassrls`/
  `rolreplication` validados em leitura (fail-closed), e o `ALTER ROLE` limita-se aos
  atributos que um administrador gerenciado com `CREATEROLE` pode alterar.

### Correção TDD e verificação

- RED: a CLI foi exercitada em PostgreSQL 16 real por um administrador com
  `CREATEROLE`, `BYPASSRLS` e `REPLICATION`, mas sem `SUPERUSER`, reproduzindo exatamente
  `Only roles with the SUPERUSER attribute may change the SUPERUSER attribute`.
- GREEN: `provision_runtime_role()` deixou de pedir mudanças de atributos reservados.
  Após criar a role com defaults seguros, consulta `pg_roles`, recusa qualquer
  `SUPERUSER`/`BYPASSRLS`/`REPLICATION` existente e só então aplica `LOGIN`,
  `NOCREATEDB`, `NOCREATEROLE` e a senha. O teste público também afirma todos os sete
  atributos finais e que a senha não aparece na saída.
- Segundo RED→GREEN: uma `orchestrator_runtime` preexistente com `BYPASSRLS` antes
  falhava com `InsufficientPrivilege` genérico; agora falha cedo e explicitamente,
  preservando o atributo privilegiado para que somente um superuser possa removê-lo.
- **Dois testes CLI desviaram para a `.env` real.** Sintoma: o migrate SQLite chamou
  Alembic no Neon e o teste de senha ausente tentou resolver o host fictício. Causa:
  `CLI_OFFLINE_ENV` não neutralizava `DATABASE_URL`, `MIGRATION_DATABASE_URL` e
  `ORCHESTRATOR_RUNTIME_PASSWORD`; `load_dotenv(..., override=False)` carregou os
  segredos locais. Correção: o ambiente offline fixa as três variáveis como vazias e o
  teste de senha usa esse ambiente explicitamente.
- **Primeiro gate global: 33 falhas PostgreSQL e cobertura 96,20%.** Causa: o novo teste
  fail-closed deixou a role cluster-global `orchestrator_runtime BYPASSRLS` viva e
  contaminou os testes seguintes. Correção: cleanup em `finally`; uma consulta após o
  teste confirma zero roles residuais. Nenhuma asserção de produção foi alterada.
- **Primeira abertura runtime tentou o socket local e expirou.** Causa: `DATABASE_URL`
  ainda estava vazia na `.env`; não era falha da role provisionada. A URL equivalente
  foi montada apenas em memória, com senha percent-encoded, e `Database.open()` passou
  duas vezes.

**Verificação:** gate focado com 41 testes verdes; gate global
`rtk proxy .venv/bin/python -m pytest` → **1093 passed, 2 skipped**, cobertura
**100% (6134/6134 statements)**. No Neon real, `db provision-runtime` concluiu; a role
ficou `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEDB`, `NOCREATEROLE` e
`NOREPLICATION`.

---

## ADR-D37 — Migração da Camada de Persistência PostgreSQL para SQLAlchemy 2.0 Async ORM

- **Implementado:** 17 modelos declarativos criados em `src/orchestrator/db/models.py`.
- **Repositórios Refatorados:** 100% das consultas SQL inline nativas em `admin.py`, `prompts.py`, `creators.py`, `feedback.py`, `artifacts.py`, `runs.py`, `effects.py` e `jobs.py` foram substituídas por seleções e inserções tipadas (`select()`, `delete()`, `update()`, `pg_insert().on_conflict_do_update()`).
- **Compilador & Adaptação:** Criado o método estático `Database.execute()` em `src/orchestrator/db/database.py` para compilação específica no dialecto `postgresql`, dumper JSONB automático via `json.dumps()` e expansão de parâmetros post-compile (`IN (...)` e `NOT IN (...)`).
- **Investigações & Correções no Processo de Testes:**
  - *Sintoma:* `psycopg.errors.DuplicateDatabase` nos testes PostgreSQL em paralelo/reinicio. *Causa:* Resto de base de dados de modelo `tests_tmpl`. *Correção:* Adicionada rotina de limpeza de bancos temporários.
  - *Sintoma:* `psycopg.ProgrammingError: cannot adapt type 'dict'` ao salvar runs com payloads JSONB. *Causa:* O driver `psycopg3` exige `Json()` ou `json.dumps()` para dicionários/listas compiladas em `params`. *Correção:* Tratamento centralizado em `Database.execute()`.
  - *Sintoma:* `psycopg.errors.UndefinedColumn: column "__" does not exist` na exclusão com `item_id.notin_()`. *Causa:* SQLAlchemy 2.0 gera cláusulas *post-compile* (`[POSTCOMPILE_...]`) para `IN`/`NOT IN`. *Correção:* Resolução explicita de `compiled._process_parameters_for_postcompile(compiled.params)` em `Database.execute()`.
  - *Sintoma:* `test_resaved_run_is_updated_and_becomes_latest` ordenando incorretamente. *Causa:* O `on_conflict_do_update` de `RunFeedback` e `Creator` não estava atualizando a coluna `position`. *Correção:* Adicionado `"position": stmt.excluded.position` no `set_` de conflito.

**Verificação Final:**
- Suíte completa de testes (`rtk proxy .venv/bin/python -m pytest --no-cov`): **1093 passed, 2 skipped** (100% verde).
- Execução E2E mock (`orchestrator run --offer "Serum X SQLAlchemy ORM" --config-dir config-mock`): 12/12 itens produzidos e persistidos no PostgreSQL sem erros.

---

## Investigação — creators aprovados sem imagem/voz no web

- **Sintoma:** após continuar o fluxo web com creators, a UI não mostrava creator com
  imagem e voz tocável. Nos runs ativos investigados (`web-88c64f21`, `web-e3eaddf7`,
  `web-8ea32f1f`), o payload do job tinha `seed_creator=true`, `approve_creators=true`
  e `edit_concepts=true`, mas o banco não tinha linhas em `creators` para esses run ids.
- **Causa:** o caminho de reutilização por `seed_creator` não constrói roster novo e,
  quando o front da galeria inicia draft, envia `approve_creators=false`; logo não há
  gate `approve_creators` para gravar `record_creators`. Além disso, áudio baixável do
  adapter live era persistido só em `voice_id`, sem preencher `voice_preview_uri`, e o
  front só renderizava `<audio>` para caminhos locais iniciados por `/`.
- **Correção:** o worker durável passa a registrar o `seed_creator` canônico como
  creator aprovado do novo run no execute/resume quando não há gate de aprovação; a normalização comum
  promove URIs de áudio tocáveis para `voice_preview_uri`; `persist_creator_media`
  preenche `voice_id`, `voice_ref`, `voice` e `voice_preview_uri`; o front usa um helper
  único que aceita `/media`, `https`, `data:audio` e `blob` para preview de voz.
- **Verificação:** RED reproduzido em
  `test_downloadable_creator_voice_becomes_playable_preview` e
  `test_record_creators_uses_renderable_voice_uri_as_preview`; GREEN com
  `rtk proxy .venv/bin/python -m pytest --no-cov` nos testes focados de mídia/store/web
  (`7 passed`). `front`: `npm run typecheck`, `npm run check:boundaries` e
  `npm run build` passaram. O teste PostgreSQL do worker ficou bloqueado localmente no
  setup (`127.0.0.1:5432` exigiu senha), antes de executar as asserções.

---

## Investigação — psycopg `another command is already in progress` no runner web

- **Sintoma:** durante polling do front e execução do runner embutido, os logs mostravam
  rollback psycopg ignorado por `another command is already in progress`, descarte de
  conexão `BAD`, `couldn't stop task 'pool-*-worker-0' within 5.0 seconds` e
  `web embedded runner falhou ao processar job`.
- **Causa:** `run_worker_once()` cancelava a task de heartbeat no `finally`. Se o
  cancelamento caísse enquanto `jobs.renew()` estava executando SQL, a conexão recebia
  `CancelledError` no meio do comando e o rollback subsequente concorria com a query
  ainda em progresso. O runner embutido também chamava `run_worker_once()` sem passar o
  pool compartilhado da API, criando e fechando um novo `AsyncConnectionPool` em cada
  poll/job.
- **Correção:** o heartbeat agora para por sinal cooperativo (`asyncio.Event`) e espera
  o `renew()` em andamento terminar, sem cancelar a operação psycopg. `run_worker_once()`
  aceita `database`/`tenant` opcionais, e o runner embutido reutiliza o
  `get_shared_database()` resolvido no lifespan da API.
- **Verificação:** RED→GREEN em
  `test_worker_stops_heartbeat_without_cancelling_inflight_renew` e
  `test_embedded_runner_reuses_shared_database`; regressões focadas de lifespan/runner:
  `3 passed`; `py_compile` de `worker.py` e `web/server.py` passou. Testes que exigem
  PostgreSQL local continuam bloqueados no sandbox por autenticação em `127.0.0.1:5432`.

---

## Implementação — progresso observável da pipeline e atividade semântica

- **Sintoma:** Campaign Detail e Operations exibiam apenas eventos técnicos efêmeros.
  Ao recarregar, o usuário não conseguia distinguir estágios concluídos, o trabalho
  atual, os processos paralelos por clip nem gates aguardando sua ação.
- **Causa:** não havia um read model canônico de progresso. O stream do LangGraph era
  traduzido somente para `node_start`/`node_end`, o frontend reconstruía estado local
  incompleto e Recent Activity usava texto técnico/horário do navegador. No modo local,
  o `event_id` também não era enviado como campo SSE `id:`, impedindo resume preciso.
- **Correção:** criado `orchestrator.progress`, que mapeia nodes para estágios estáveis,
  correlaciona start/end por `operation_id`, preserva `item_id`/tentativa e projeta
  `progress` e `activity`. O runner entrega esses eventos por `event_sink`; o worker
  durável persiste cada evento em `run_events` antes da publicação; `/api/state`
  reidrata o snapshot e a timeline. O modo local ganhou IDs/horários estáveis, replay
  após `Last-Event-ID` e emissão SSE `id:`. O frontend passou a hidratar REST + SSE com
  deduplicação, mostrar checks duráveis, todos os processos paralelos ativos, gates,
  contadores, estágio/tentativa por clip e atividade com horário do servidor.
- **RED API:** `/api/state` não retornava `progress`; o teste falhou com `KeyError`.
  **GREEN:** projeção canônica adicionada sem alterar os contratos de gate.
- **RED fan-out:** concluir um clip encerrava visualmente o estágio enquanto outro
  permanecia ativo. **GREEN:** operações são correlacionadas individualmente e o estágio
  só conclui ao atingir `total_units`.
- **RED SSE local:** reconectar com `Last-Event-ID: local-1` repetia `local-1` e o
  stream não continha `id: local-2`. **GREEN:** replay filtrado e ID SSE explícito.
- **RED frontend:** estágio global ativo aparecia como `1 clip in Concepts`.
  **GREEN:** contagem de clips ficou restrita aos estágios por item; estágios globais
  usam `Concepts in progress`. O mesmo ciclo revelou DOM acumulado entre testes porque
  Vitest não tinha cleanup global; `afterEach(cleanup)` foi adicionado ao setup.
- **RED atividade:** o smoke test ainda projetava 106 eventos brutos como entradas
  repetidas de Concepts, Creators e Assembly. **GREEN:** a timeline mantém a primeira
  abertura e somente a conclusão canônica de cada estágio/item/tentativa, descartando
  `item_update` técnico quando há `progress_event`; o mesmo run passou a expor 34
  entradas operacionais sem perder os dois clips paralelos.
- **Falha TypeScript:** a primeira implementação de `gateRef` exigia
  `Record<string, unknown>`, incompatível com variantes discriminadas do stream.
  **Correção:** o helper recebe somente a estrutura opcional necessária e mantém o
  narrowing de `gate_type`.
- **Falhas de comando sem mudança de produto:** `python` e `ruff` não estão instalados
  diretamente no ambiente, então Python foi executado por `uv run`; lint Ruff não pôde
  ser executado e foi substituído por `compileall` mais os gates de teste/tipo. Um
  filtro Vitest incluiu `front/` apesar do cwd já ser `front/`, e um gate referenciou o
  arquivo inexistente `tests/test_worker.py`; ambos foram corrigidos para os caminhos
  reais antes da verificação final.
- **Cobertura focada inicialmente falhou:** o `addopts` global exige 100% do projeto
  mesmo ao selecionar um único teste. Os ciclos red/green usaram `--no-cov`, seguidos
  de uma execução isolada de `orchestrator.progress` com **244/244 statements (100%)**.
- **Suíte global:** `1004 passed, 2 skipped, 113 errors`; os 113 erros ocorreram no
  setup das fixtures PostgreSQL antes das asserções, porque `127.0.0.1:5432` exigiu
  senha. Nenhuma falha funcional apareceu fora desse bloqueio de infraestrutura.
- **Verificação final:** backend focado, incluindo projeção, runner E2E, REST/SSE,
  execução web local, URLs assinadas e serviços de runner: **128 passed**. O módulo
  `orchestrator.progress` manteve **258/258 statements (100%)**. Frontend:
  **3 passed**, `tsc --noEmit`, boundaries e build Vite verdes. `compileall` e
  `git diff --check` também passaram. Smoke real em `config-mock`, batch 2: fase
  `done`, 9 estágios completos, 2/2 clips montados e 34 atividades semânticas.

---

## Implementação — Pipeline V2, agents criativos e revisão única

- **Sintoma:** o usuário não sabia o que já havia terminado, qual processo estava
  executando nem o que seria solicitado em seguida. A topologia pública misturava
  persona, dois gates humanos e nodes técnicos; concepts/scripts/video tinham
  autoridades agentic diferentes e contratos de saída inconsistentes.
- **Causa:** o grafo interno era usado como modelo de UX. Dados da campanha podiam
  aparecer próximos do prompt, outputs criativos não tinham um schema terminal comum e
  tracing preservava offer/script quando a flag de redação estava desligada.
- **Correção de produto:** a API/UI V2 expõe Configuração → Plano criativo → Revisão →
  Produção e QC → Montagem. Há um único interrupt `review_creative_plan`; regeneração
  volta a concepts/scripts/creator_profiles e aprovação inicia o fan-out. O wizard pede
  offer, público, fatos/restrições, direções opcionais, plataforma, objetivo, batch e
  performance; a revisão mostra conceitos+roteiros e exatamente dois creators.
- **Correção dos agents:** criados contratos Pydantic estritos para campanha,
  concepts, scripts e casting. Apenas `concepts`, `scripts` e `creator_profiles` podem
  ser agents; cada um tem prompt e hash próprios, allowlist de uma tool e uma submissão
  `creative-v2`. Persona saiu do top graph e vídeo voltou a tool/adapters automáticos.
- **Prompt injection:** a prioridade é controles do servidor → política compartilhada
  → contrato de fase → dados. Campanha, performance, feedback e outputs são
  `UNTRUSTED_STAGE_DATA`. IDs, counts, routing, budgets e providers permanecem
  server-owned. API/traces expõem apenas versão/hash/schema, nunca corpo/path, offer,
  script ou mensagens.
- **Revisão:** patches aninhados usam `extra=forbid`; concept IDs, dois creator IDs,
  offer e mídia são preservados. Só copy e direção criativa conhecidas podem mudar.
  Runs duráveis exigem `gate_id`, `version` e `gate_type=review_creative_plan`.
- **Progresso:** eventos customizados reais do LangChain informam scripts `N/total` e
  previews `N/2`; REST/SSE e Recent Activity projetam as cinco fases e os trabalhos
  internos relevantes sem expor detalhes de prompt.
- **Cancelamento:** a migration `20260728_0009` adicionou estados `review` e
  `cancelled`, cancelou gates V1 pendentes e bloqueia resolução posterior com 410. Foi
  aplicada na base configurada em 2026-07-28: **3 gates**, **3 runs** e **0 jobs**
  associados foram cancelados.
- **Falhas investigadas:** o perfil live com providers mock falhou porque o mock antigo
  não submetia schemas terminais; ele agora produz os três contratos V2
  deterministicamente. A revisão web descartava briefs dos creators e aceitava campos
  arbitrários; a normalização passou a preservar os briefs e o merge ganhou allowlists.
  A API durável também resolvia o `gate_id` sem conferir o `run_id` da URL e o worker
  podia persistir creators antes do merge seguro; a API agora compara o gate pendente
  completo e materializa uma resolução sanitizada sobre o payload canônico antes de
  enfileirar o resume. O índice de runs classificava cancelamentos como erro por causa
  do motivo de auditoria em `runs.error`; `cancelled` agora é uma coleção própria na
  API/UI e esses runs não oferecem retry.
  Testes antigos de persona/video-agent/dois gates foram atualizados porque
  especificavam comportamento deliberadamente substituído pela D38.
- **Verificação:** todos os testes backend sem fixture PostgreSQL passaram; a suíte
  PostgreSQL continua bloqueada no runner local de pytest por autenticação em
  `127.0.0.1:5432`, mas a migração real chegou a `head`. Frontend: Vitest verde,
  `tsc --noEmit`, boundaries e build Vite verdes. `compileall` e `git diff --check`
  passaram; Ruff não está instalado no venv.
- **Documentação:** `docs/PIPELINE_V2.md`,
  `docs/ADR-D38-pipeline-v2-agent-contracts.md`, D38 em `docs/DECISIONS.md` e regras
  canônicas atualizadas em `AGENTS.md`.

---

## Correção — imagens dos creators na revisão criativa

- **Sintoma:** a revisão V2 mostrava os dois players de voz, mas os containers de
  imagem dos creators ficavam vazios.
- **Reprodução real:** o run `web-e4f748bd` chegou ao gate com dois previews; os
  objetos `creator-0/image.png` e `creator-1/image.png` existiam no R2, enquanto o DOM
  não continha nenhum `<img>` e continha os dois áudios assinados.
- **Causa:** o roster interno usa `upscaled_base`. No caminho local,
  `_execute_run()` convertia esse alias para `image_uri`, mas o worker durável abria
  `review_creative_plan` com o interrupt bruto. `/api/state` e o replay SSE também
  devolviam gates antigos sem normalizar os creators. A voz aparecia porque a
  persistência já materializava `voice_preview_uri`.
- **Correção:** `normalize_creator_payload()` virou a projeção pública comum.
  Novos gates duráveis são persistidos com `image_uri`/`image`; gates e eventos
  antigos são normalizados durante leitura/replay. Campos internos como
  `upscaled_base` não fazem parte do payload público.
- **RED → GREEN:** três regressões cobrem gate persistido, evento SSE persistido e
  payload aberto pelo worker. `tests/test_web_endpoints.py`: **65 passed**; conjunto
  focado web/API/store: **112 passed**.
- **Infra de teste:** os sete testes de `test_postgres_creators.py` não chegaram às
  asserções porque não há PostgreSQL em `127.0.0.1:5432` neste ambiente
  (`Connection refused`). Nenhuma asserção foi alterada ou ignorada.

---

## Correção — esgotamento do pool ao regenerar creators

- **Sintoma:** durante a regeneração, `/api/runs`, `/api/creators`, `/api/state` e
  `/api/stream` passavam a responder `500` após 30 segundos com
  `psycopg_pool.PoolTimeout`. O processo também registrava rollback recusado dentro de
  `Transaction`, fechava conexões `ACTIVE` e as devolvia ao pool como `BAD`.
- **Causa transacional:** `AsyncConnectionPool.connection()` já controla
  commit/rollback, mas `Database.connection()` abria uma segunda `Transaction`
  manual. Se uma query era cancelada, a saída da transação interna podia falhar; a
  saída externa tentava um segundo rollback ainda dentro da primeira transação,
  produzindo `Explicit rollback() forbidden within a Transaction context`.
- **Amplificação:** cada abertura de repository chamava `resolve_tenant()`, que
  repetia três writes idempotentes para materializar organização, usuário e membership.
  Requests concorrentes da UI disputavam a mesma linha do tenant; uma limpeza de
  conexão interrompida deixava as quatro vagas do pool ocupadas por trabalho
  serializado ou por reposição de conexões.
- **Correção:** removida a transação manual; o contexto do pool voltou a ser o único
  dono da transação. Em `CancelledError`, a conexão ainda é fechada antes da saída para
  impedir rollback sobre uma query `ACTIVE`, e o pool a repõe. No modo local, tenants
  resolvidos são armazenados por identidade com lock e double-check, então o bootstrap
  ocorre uma vez por processo. No modo `cloudflare_access` não há cache: toda request
  revalida a membership.
- **RED → GREEN:** o teste de cancelamento primeiro falhou porque
  `connection.transaction()` ainda era chamado; oito resoluções concorrentes fizeram
  oito bootstraps. Após a correção, as três regressões de transação, bootstrap único e
  reautorização Access passaram.
- **Verificação:** conjunto focado web + item updates + regressões PostgreSQL:
  **92 passed**. Contra o Neon, uma query `pg_sleep` cancelada foi descartada e a
  próxima `SELECT 1` concluiu no mesmo pool. Em carga real, 48 requests concorrentes
  retornaram `200`; depois de cancelar seis streams SSE, outras 12 requests concorrentes
  também retornaram `200`, sem novos avisos de rollback ou `PoolTimeout`.
- **Run afetado:** `web-e4f748bd` voltou para `review`, sem erro, com os dois creators.
  As duas URLs assinadas de imagem responderam `200 image/png`.

---

## Correção — envio duplicado da revisão criativa

- **Sintoma:** ao aprovar ou pedir regeneração na revisão, o primeiro
  `POST /api/v2/runs/{run_id}/review` concluía com `200`, mas um segundo envio imediato
  reutilizava o mesmo `gate_id`/`version` e recebia `409 Conflict`. A tela permanecia
  visualmente acionável enquanto aguardava a atualização do estado da campanha.
- **Causa:** o painel dependia apenas do estado assíncrono da mutation para bloquear a
  ação. Cliques próximos podiam atravessar antes do próximo render, somente o botão
  selecionado indicava envio e, após o `200`, o painel antigo continuava montado até o
  polling/SSE refletir a resolução do gate. O cliente HTTP também descartava o status
  estruturado, impedindo tratar `409` como estado concorrente esperado.
- **Correção:** o painel ganhou lock síncrono por instância, desabilita todas as ações
  durante o envio e substitui os controles por confirmação local após sucesso. O cliente
  agora lança `HttpError` com `status`/`detail`; um `409` mostra mensagem neutra de revisão
  já processada e força a revalidação do run, sem apresentar falha genérica. A mutation
  invalida as queries também no caminho de erro e o painel é remontado somente quando
  muda o par `gate_id:version`, liberando um gate realmente novo.
- **RED → GREEN:** os testes inicialmente reproduziram duas mutations para clique
  duplicado, ausência de confirmação, perda do status HTTP e falta de invalidação no
  conflito. Foram adicionadas regressões para lock global, retry após erro comum,
  tratamento do `409`, remount por nova versão do gate e preservação de `status/detail`.
- **Verificação:** frontend com **10 testes Vitest verdes**, `tsc --noEmit`,
  `check:boundaries` e build Vite concluídos. O `402 Payment Required` observado depois
  da aprovação pertence ao saldo do provider de geração e não ao contrato de revisão.

---

## Correção — cotas externas durante produção live

- **Sintoma:** após várias gerações de vídeo concluídas, a Replicate respondeu
  `402 Payment Required`. Na sequência, o Neon encerrou conexões com
  `Your project has exceeded the data transfer quota`; SSE e `/api/state` passaram a
  expor `AdminShutdown`, `PoolTimeout` e `500`.
- **Causa externa:** Replicate e Neon atingiram limites independentes. O `402` não é
  retentável sem adicionar saldo. O PostgreSQL indisponível impediu inclusive que o
  worker persistisse imediatamente a falha do job.
- **Amplificação interna:** cada `progress_event` recebido por SSE agendava um novo
  `GET /api/state`. Esse endpoint lê checkpoint e timeline persistida; durante fan-out
  de vídeo, a UI repetia transferências que não eram necessárias porque o próprio SSE
  já contém o progresso e o reducer atualiza a tela.
- **Correção do worker:** falhas HTTP permanentes `4xx` de provider, incluindo `402`,
  encerram o job sem retry automático. `ReadError`/`ReadTimeout` e
  `WriteError`/`WriteTimeout` também não são retentados no nível do job, pois a chamada
  paga pode ter sido aceita antes da falha de resposta. `429` e `5xx` continuam
  retentáveis.
- **Correção da API:** erros psycopg/pool antes da resposta viram `503` sanitizado com
  `Retry-After: 30`; o SSE em andamento emite `service_unavailable` e reconecta após
  30 segundos sem traceback ASGI. `/readyz` agora verifica o PostgreSQL com timeout
  curto e retorna `not-ready` quando a persistência está indisponível.
- **Correção do front:** eventos de progresso e atualização de item são reduzidos
  localmente sem refetch integral do run. Fetch completo permanece na hidratação e em
  transições que realmente exigem revalidação.
- **RED → GREEN:** regressões reproduziram retry indevido de `402`/timeout pós-envio,
  exceção dentro do stream, readiness falsamente verde e o segundo `getRunState` após
  um único evento de progresso.
- **Verificação:** backend web/retry com **102 testes verdes** e worker focado com
  **3 testes verdes**; frontend com **11 testes Vitest verdes**, TypeScript,
  boundaries e build Vite verdes; `compileall` concluído.
- **Ação operacional obrigatória:** adicionar crédito na Replicate e liberar/resetar
  a transferência do projeto Neon antes de reiniciar a API live. Código não consegue
  contornar cotas impostas pelos providers.

---

## Mudança — Kling/Seedance via Vercel; Replicate somente para voz

- **Objetivo:** remover geração de vídeo do Replicate sem alterar o contrato
  `VideoPort` nem os nodes do LangGraph.
- **Problema encontrado:** `config/providers.yaml` apontava `video: replicate`; o
  adapter real só implementava o tier `ltx`, e `node_product_demo` ainda enviava
  `tier="ltx"` literalmente. Portanto, trocar apenas o YAML não habilitaria Kling ou
  Seedance.
- **Correção:** criado `VercelGatewayVideoAdapter`, que escolhe o model id pelo tier e
  reutiliza o bridge AI SDK 6. O live usa Kling 3.0 I2V para talking-head, Seedance 2.0
  para product demo e Seedance 2.0 para montagem. O tier de product demo passou a ser
  configurável. O alias live `creator_vercel_replicate_voice` explicita que Replicate
  ficou somente no sub-adapter de voz ElevenLabs do creator.
- **RED → GREEN:** os testes falharam inicialmente por ausência do módulo
  `orchestrator.adapters.vercel_gateway_video`; após adapter, registry, config e
  roteamento, o conjunto focado passou.
- **Operação:** exige `npm install`, `AI_GATEWAY_API_KEY` (ou `VERCEL_OIDC_TOKEN`) e
  acesso pago a vídeo no Vercel AI Gateway. `REPLICATE_API_TOKEN` e
  `REPLICATE_ELEVENLABS_MODEL` continuam necessários somente para voz.
- **Verificação:** **111 testes focados passaram**, incluindo graph/nodes/tools,
  registry, config live, tracing e ambos os adapters Vercel; o módulo novo ficou com
  **100% de cobertura**. `node --check`, `compileall` e `git diff --check` passaram.
  Na suíte completa, **1061 testes passaram e 2 foram pulados**; 114 testes PostgreSQL
  falharam no setup porque não há servidor em `127.0.0.1:5432`, a limitação de
  infraestrutura local já documentada no projeto. Não houve falha funcional adicional.

---

## 2026-07-29 — modo dev local com PostgreSQL 16 e R2

- **Entrega:** `./scripts/dev-local up|down|reset --yes` agora controla PostgreSQL,
  migração, API com runner embutido e Vite com HMR. O Compose fixa as URLs internas em
  `postgres:5432`, publica o banco somente em `127.0.0.1:55432`, carrega secrets do
  `.env` sem permitir que uma URL Neon sobrescreva o banco local e preserva o volume do
  PostgreSQL em `down`. `reset --yes` remove apenas volumes locais; R2 não faz parte do
  conjunto de volumes.
- **Storage:** a resolução foi centralizada em
  `ORCH_DEV_STORAGE_BACKEND` → `STORAGE_BACKEND` → `providers.yaml`. API, adapters e
  `/readyz` usam a mesma função. R2 é o padrão do comando dev; filesystem continua
  disponível com `ORCH_DEV_STORAGE_BACKEND=local`.
- **Imagem:** o `Dockerfile` inclui `alembic.ini` e `migrations/`; o serviço `migrate`
  precisa concluir antes da API. O perfil live usa Vercel AI Gateway para
  LLM/imagem/Kling/Seedance, Replicate somente para voz e R2 para mídia.
- **Preflight:** o wrapper detecta Compose V2 ou V1, valida Docker, config e apenas as
  credenciais exigidas pelo perfil/backend selecionado. Mensagens listam nomes de
  variáveis ausentes, nunca seus valores.

### Falhas investigadas

- **Sintoma:** os primeiros testes encontraram `app`/MinIO e nenhuma ordem de migração.
  **Causa:** o Compose ainda representava o ambiente legado. **Correção:** serviços
  reestruturados para `postgres`, `migrate`, `api` e `front`, com healthcheck,
  dependências e volumes explícitos.
- **Sintoma:** override local ainda construía S3/R2 e `/readyz` validava o backend
  errado. **Causa:** factory e readiness implementavam precedências diferentes.
  **Correção:** `resolve_storage_backend()` virou a fonte única e ganhou regressões de
  precedência.
- **Sintoma:** o preflight falhou no `mawk`. **Causa:** o parser usava uma forma de
  `if` multilinha não portátil. **Correção:** leitura do `.env` reescrita em sintaxe
  POSIX aceita pelo awk disponível, mantendo secrets fora da saída.
- **Sintoma:** Compose V1 retornou `KeyError: ContainerConfig`, inclusive ao repetir
  `up` depois de reconstruir a imagem. **Causa:** `docker-compose 1.29.2` tenta ler o
  campo removido de metadata de containers criados pela imagem anterior.
  **Correção:** no V1, `up` executa primeiro `down --remove-orphans`; containers são
  efêmeros e os named volumes permanecem intactos. A regressão simulada ficou
  RED antes da mudança e o segundo `up` real passou depois dela.
- **Sintoma:** PostgreSQL registrou incompatibilidade de collation após trocar a
  imagem. **Causa:** `postgres:16-bookworm` usava glibc 2.36 sobre um volume criado por
  glibc 2.41. **Correção:** imagem `postgres:16`, compatível com o volume existente.
- **Sintoma:** 99 testes PostgreSQL falharam por SCRAM. **Causa:** helpers de teste
  omitem senha, enquanto o servidor local exige autenticação. **Correção operacional:**
  suíte executada com `PGPASSWORD=postgres`; nenhuma asserção ou política de segurança
  foi afrouxada.
- **Sintoma:** `orchestrator migrate --database-url ...` escolheu a URL remota do
  `.env`. **Causa:** `MIGRATION_DATABASE_URL` carregada depois tinha precedência sobre
  a flag explícita. **Correção:** a opção CLI explícita agora vence o ambiente.
- **Sintoma:** quatro testes de gate esperavam os dois gates V1. **Causa:** ficaram
  obsoletos após D38, que define exatamente um `review_creative_plan`.
  **Correção:** testes migrados para gate combinado, edição versionada, conflito stale
  e retomada V2; código inalcançável dos gates antigos foi removido do runner.
- **Sintoma:** a suíte comportamental ficou verde, mas o gate de cobertura parou em
  97,81% e depois 98,49%. **Causa:** novos ramos defensivos e um método duplicado
  sombreado em `db/admin.py`. **Correção:** duplicata e branches inalcançáveis
  removidos; validações, cancelamento, heartbeat, contratos criativos e erros
  versionados receberam testes específicos.
- **Sintoma:** cinco regressões novas falharam no primeiro passe. **Causas:** formato
  real do logger usa largura de campo; `prompt` é sempre removido antes da redação;
  Pydantic aplica `str_strip_whitespace` antes do `min_length`; e dois testes do runner
  não configuravam a identidade tenant. **Correções:** expectativas alinhadas ao
  contrato real, validador redundante removido e identidade de teste explicitada.

### Aceite durável

- Compose V1 real construiu a imagem, aplicou Alembic até `20260728_0009`, deixou o
  PostgreSQL healthy, respondeu `/readyz` com `storage=r2` e serviu Vite em `:5173`.
- Smoke `config-staging`, batch 1: run `web-72d047ee` abriu o único gate combinado;
  SSE registrou a sequência até `awaiting_review`; reiniciar a API preservou
  `gate_id`/`version`; aprovação retomou e concluiu `done` com 1/1.
- O banco persistiu apenas ponteiros `r2://` — nenhuma assinatura `X-Amz-*` apareceu
  em state/eventos. Cinco objetos do run permaneceram no R2 após `down` e nova subida,
  confirmando que o ciclo local não toca no bucket.
- **Live batch 1:** o run `web-8fcf4d65` usou somente
  `ai-gateway.vercel.sh`, `api.replicate.com`, `replicate.delivery` e o endpoint S3 do
  R2. Conceitos, roteiro, dois perfis e duas imagens retornaram `200` pelo Vercel;
  duas vozes ElevenLabs retornaram `201` pelo Replicate e foram baixadas; quatro
  objetos canônicos apareceram no R2. A leitura pelo repository confirmou
  `r2_pointer=True` e `signed_url=False` no PostgreSQL.
- **Bloqueio externo do live:** depois da aprovação, a primeira geração Kling foi
  recusada pelo Vercel com `Video generation requires a minimum balance of $1`.
  O run terminou corretamente em `error`, sem retry ambíguo ou fallback mock. É
  necessário adicionar saldo no Vercel AI Gateway para concluir vídeo/QC/montagem;
  não houve defeito de roteamento local a corrigir. Ao fim do aceite, o Compose voltou
  para `config-staging`, `/readyz` respondeu `ready/r2` e o PostgreSQL continuou
  healthy.
- **Verificação final:** `1246 passed, 2 skipped`, cobertura obrigatória de
  **100,00%** (`7614` statements, zero ausentes). `docker-compose config -q`,
  `bash -n scripts/dev-local`, build da imagem/frontend e `git diff --check`
  concluíram sem erro. Os quatro warnings restantes são depreciação do wrapper
  LangSmith e coroutines internas do LangGraph em testes já verdes.

---

## Mudança — clips live no Seedance 2.0 Fast (2026-07-29)

- **Objetivo:** gerar todos os clips intermediários live com
  `bytedance/seedance-2.0-fast`, preservando a montagem final no
  `bytedance/seedance-2.0` Standard.
- **RED:** `test_live_config_uses_seedance_fast_for_all_clips` esperou um único tier
  `seedance` Fast e falhou porque o perfil ainda continha Kling 3.0 I2V para
  talking-head e Seedance 2.0 Standard para product demo.
- **Correção:** `config/pipeline.yaml` passou a expor somente o tier `seedance`, com
  modelo Fast, `cost_per_second=0.1344` e concorrência 4. Como o roteador escolhe o
  primeiro tier e `video.product_demo_tier=seedance`, talking-head, regenerações de
  QC e product demo compartilham obrigatoriamente o mesmo modelo. O adapter e o
  grafo não precisaram mudar.
- **Preço estimado:** o catálogo Vercel publica 5,60 por milhão de tokens para Fast
  contra 7,00 para Standard; a estimativa por segundo preserva a mesma proporção de
  80% aplicada ao valor Standard anterior de 0,168 USD/s.
- **Falha de verificação investigada:** o primeiro comando focado referenciou
  `tests/test_graph_routing.py`, arquivo inexistente. O teste correto é
  `tests/test_routing.py`; o comando corrigido executou normalmente.
- **GREEN focado:** live config, adapter Vercel e routing concluíram com
  **17 passed**. `load_pipeline("config")` confirmou clips Fast e assembly Standard;
  `git diff --check` passou.
- **Falha da suíte completa investigada:** a primeira execução acumulou 115 erros de
  fixture antes das asserções e cobertura parcial de 87,89%. A causa foi a ausência
  do PostgreSQL esperado pelos testes em `127.0.0.1:5432`; o banco dev fica em
  `55432` e usa um papel runtime sem privilégios para criar os bancos/roles isolados
  da suíte. A verificação foi repetida contra um container PostgreSQL 16 efêmero em
  `5432`, com credenciais apenas de teste, sem alterar asserções ou usar
  `skip`/`xfail`. O container foi removido imediatamente depois.
- **GREEN completo:** toda a suíte concluiu com código 0 e cobertura obrigatória de
  **100,00%** (`7614` statements, zero ausentes). Permaneceram apenas os quatro
  warnings já conhecidos de depreciação do LangSmith e coroutines internas do
  LangGraph em testes verdes.

---

## Mudança — clips live retornam ao Replicate com PrunaAI (2026-07-29)

- **Diagnóstico do run:** `web-e68546b9` terminou em `phase=error` depois de cinco
  tentativas. A causa terminal foi o Vercel AI Gateway exigir saldo mínimo de 10 USD
  para vídeo. Um `429` anterior do Replicate/ElevenLabs foi transitório e recuperou
  para `201`; os warnings `psycopg ... connection not in pipeline mode` ocorreram na
  limpeza das tentativas, mas não causaram a falha.
- **RED 1:** o contrato live passou a esperar `video: replicate` e falhou porque
  `providers.yaml` ainda selecionava `vercel_gateway_video`.
- **GREEN 1:** o papel `video` voltou ao adapter Replicate, mantendo LLM/imagem e
  montagem nos adapters Vercel já existentes.
- **Tracer intermediário:** a volta inicial foi validada com LTX 2.3 Fast; live
  config, adapter Replicate, composite e routing concluíram com **38 passed**.
- **RED 2:** o contrato público do `prunaai/p-video` falhou porque o adapter Replicate
  ainda tratava todo modelo diferente do LTX como fallback não configurado.
- **GREEN 2:** o adapter ganhou input explícito para P-Video, usando `save_audio`
  em vez de `generate_audio`, draft desligado, prompt upsampling desligado e imagem
  de referência preservada.
- **RED 3:** o contrato live passou a exigir `product_demo_tier=pruna` e falhou
  porque a configuração ainda selecionava `ltx`.
- **GREEN 3:** talking-head, regenerações de QC e product demo usam o único tier
  `pruna`, modelo `prunaai/p-video`, custo oficial de 0,04 USD/s em 1080p, 24 FPS
  e concorrência 1. O throttle Replicate continua global e compartilhado com a voz.
- **Verificação final:** os testes focados concluíram com **39 passed**. A suíte
  completa passou com código 0 e cobertura obrigatória de **100,00%** (`7619`
  statements, zero ausentes) contra PostgreSQL 16 efêmero, removido ao final.
- **Runtime local:** Compose V1 foi recriado preservando os volumes PostgreSQL e sem
  tocar no R2. `/readyz` respondeu `ready/r2`; a configuração carregada dentro da
  API confirmou `video=replicate`, `tier=pruna`, `model=prunaai/p-video` e 24 FPS.
  Nenhum novo run pago foi disparado durante a verificação.

---

## Mudança — montagem PrunaAI para validação E2E de baixo custo (2026-07-29)

- **Objetivo:** remover a última dependência de vídeo do Vercel e validar clips,
  QC, montagem e persistência usando Replicate/PrunaAI.
- **RED 1:** o teste público de `assemble()` falhou porque
  `ReplicateVideoAdapter` ainda não aceitava configuração de montagem.
- **GREEN 1:** o adapter passou a implementar `AssemblyPort` com o contrato
  `prunaai/p-video`, imagem do creator, prompt final, 1080p, 24 FPS, sem áudio e
  draft. Output nulo/vazio continua falhando antes de criar artifact.
- **RED 2:** o contrato live esperou `assembly: replicate` e falhou porque
  `providers.yaml` ainda selecionava `vercel_seedance_assembly`.
- **GREEN 2:** clips e montagem compartilham a mesma instância Replicate e o throttle
  global, evitando concorrência adicional com ElevenLabs.
- **RED 3:** o perfil live esperou draft a 0,01 USD/s e falhou enquanto o tier ainda
  estava em 0,04 USD/s sem draft.
- **GREEN 3:** talking-head, product demo e montagem custam 0,08 USD cada para 8s;
  o piso de vídeo por item sem retries é 0,24 USD.
- **RED 4:** o teste de custo da montagem falhou com `KeyError: cost_usd` porque o
  artifact registrava o custo, mas `node_assembly` não o somava ao item.
- **GREEN 4:** a montagem agora incrementa `Item.cost_usd`, então o custo aparece no
  resumo do run. A suíte focada concluiu com **123 passed**.
- **Verificação completa:** toda a suíte passou com código 0 e cobertura obrigatória
  de **100,00%** (`7645` statements, zero ausentes) contra PostgreSQL 16 efêmero,
  removido ao final.
- **Runtime local:** Compose V1 foi reconstruído preservando PostgreSQL/R2.
  `/readyz` respondeu `ready/r2`, os logs de startup não registraram erro e a API
  carregou `video=replicate`, `assembly=replicate`, `prunaai/p-video`, draft 1080p
  e custo de 0,01 USD/s. Nenhum run pago foi iniciado automaticamente.

---

## Correção — QC aceita ponteiros canônicos de vídeo R2/S3 (2026-07-29)

- **Sintoma:** o run `web-4f1f7fcf` gerou e persistiu seis clips por item com
  PrunaAI, mas todas as tentativas de QC terminaram em
  `clip_N_invalid_video_uri`; os dois itens foram descartados, a montagem não
  executou e o run acumulou 0,96 USD.
- **Causa:** `persist_item_media()` substitui a URL temporária do provider pelo
  ponteiro canônico `r2://bucket/key.mp4`. O `IntegrityQCAdapter` aceitava
  HTTP(S), data URI e paths locais, mas rejeitava qualquer outro esquema antes
  da camada de saída derivar a URL assinada.
- **RED → GREEN:** a primeira regressão pública reproduziu a reprovação de um
  `r2://...mp4`. O QC passou a aceitar ponteiros `r2://` e `s3://` somente quando
  bucket, objeto e extensão de vídeo são válidos. Imagens, objetos sem extensão,
  ponteiros incompletos, provider mock e `fallback_reason` continuam reprovados.
- **Regressão de integração:** um clip `data:video/mp4` foi persistido pelo backend
  R2 real com client S3 em memória, convertido em `r2://...mp4` e aprovado pelo
  QC, preservando `source_uri` e `storage_backend`.
- **Verificação:** QC/media store/R2 concluíram com **54 passed**. A suíte completa
  terminou com **1259 passed, 2 skipped**, cobertura obrigatória de **100,00%**
  (`7650` statements, zero ausentes) contra PostgreSQL 16 efêmero, removido ao
  final. Nenhum run pago foi iniciado.
- **Falha de rebuild investigada:** o Compose V1 concluiu o build, mas falhou ao
  recriar os containers antigos de migrate/API com `KeyError: ContainerConfig`,
  incompatibilidade conhecida do Compose V1 durante a convergência de containers.
  Foram removidos somente esses containers parados, sem apagar volumes, PostgreSQL
  ou objetos R2; a recriação seguinte concluiu normalmente.
- **Runtime local:** a nova API respondeu `/readyz` com `ready/r2`, iniciou o runner
  embutido sem erros e confirmou dentro do container: R2 MP4 e S3 WebM válidos,
  R2 JPG inválido. Nenhuma campanha ou chamada paga foi disparada.

---

## Correção — vídeo final com locução ElevenLabs e montagem FFmpeg (2026-07-29)

- **Sintoma:** o artifact `assembled` do run live continha somente stream H.264; o
  `ffprobe` não encontrou áudio. A “montagem” de D42 era uma terceira geração
  `prunaai/p-video` com `save_audio=false`, não concatenação dos dois clips.
- **Contrato de roteiro (RED → GREEN):** `write_script_tool` passou a rejeitar
  locução acima dos limites server-owned de 14 segundos ou 35 palavras.
  `node_scripts` injeta esses tetos e o agent de scripts pode fazer uma única
  correção limitada. O estimador legado inicialmente marcou CTA curta como oito
  segundos e quebrou quatro smokes CLI com `narration exceeds 14 seconds`; a causa
  foi corrigida calculando segundos pela quantidade real de palavras, sem remover
  a validação.
- **Identidade da voz (RED → GREEN):** a URL temporária do preview e a voz estável
  do provider foram separadas. `voice_ref`/`voice_model_ref` preservam a voz
  aprovada (por exemplo `Rachel`), enquanto `voice_preview_uri` aponta para o áudio
  persistido no R2. O fan-out leva a referência estável ao `Item`.
- **Locução (RED → GREEN):** o novo `node_voiceover`, executado somente após QC
  aprovado, envia o texto falado completo para `elevenlabs/turbo-v2.5` no
  Replicate (`prompt` + `voice`), persiste `voiceover` e soma o preço de
  `0,05 USD/1.000 caracteres` a `Item.cost_usd`. Falha ou ausência de voz/roteiro
  termina o item com erro explícito e nunca produz final silencioso.
- **Montagem (RED → GREEN):** `ffmpeg_assembly` recebe signed URLs somente numa
  cópia transitória, seleciona os dois clips da última tentativa, concatena
  talking-head + product demo, descarta áudio de origem, aplica loudness,
  silêncio final e aceleração máxima de 10%. O output é 16 s, H.264/AAC e só vira
  `assembled` após validação do `ffprobe`. Bytes transitórios não entram no
  checkpoint; local/R2 guardam apenas o artifact canônico.
- **Custo e API:** a montagem local custa zero e elimina a terceira geração Pruna.
  `summarize()` agora expõe `cost_by_stage` (`video`, `voiceover`, `assembly`).
  REST/SSE inclui o artifact de locução e deriva signed URL somente na saída.
- **Infra:** FFmpeg/ffprobe foram adicionados à imagem e ao readiness quando
  `assembly=ffmpeg_assembly`. A API reconstruída confirmou ambos os binários,
  `video=replicate`, `assembly=ffmpeg_assembly`, duração 16/14 s e `/readyz`
  `ready/r2`; logs de startup ficaram sem erro. Nenhuma campanha paga foi iniciada.
- **Falhas de verificação investigadas:** um comando intermediário citou
  `tests/test_config.py`, que não existe; ele foi corrigido para a lista real de
  testes. A primeira suíte funcional encontrou os quatro smokes CLI descritos
  acima e 99,33% de cobertura; após a causa ser corrigida, a segunda ficou verde
  mas revelou um único ramo sem cobertura na propagação de `StageExecutionError`.
  A regressão correspondente foi adicionada. No rebuild, Compose V1 repetiu
  `KeyError: ContainerConfig`; foram removidos somente os containers antigos de
  migrate/API, preservando PostgreSQL, volumes e R2, e a recriação concluiu.
- **Verificação final:** `1299 passed, 2 skipped`, cobertura obrigatória de
  **100,00%** (`7928` statements, zero ausentes). `docker-compose config -q`,
  `bash -n scripts/dev-local`, `compileall` e `git diff --check` também passaram.
- **Regressão visual final (RED → GREEN):** a integração extraiu um frame na
  segunda metade do MP4 e ainda encontrou o primeiro clip. O filtro aplicava
  `trim` antes de `tpad=stop_duration=<duração>`, anexando uma duração inteira
  mesmo quando o clip já estava completo; por isso o `-t` global encerrava o
  output antes da transição. O padding agora ocorre antes do `trim` final de cada
  trecho. O teste passou a exigir o frame do segundo clip e a suíte completa foi
  repetida: `1299 passed, 2 skipped`, cobertura de **100,00%**. A imagem da API
  foi reconstruída; o Compose V1 repetiu o `ContainerConfig`, resolvido removendo
  somente os containers antigos de migrate/API, sem volumes. `/readyz` voltou
  `ready/r2`, FFmpeg/ffprobe estão presentes e os logs do runner estão limpos.

---

## Correção — integração completa ElevenLabs Voice Design (2026-08-03)

### Entrega

- O perfil live passou a `creator_vercel_elevenlabs_design`; aliases antigos ficam
  apenas para compatibilidade. Mock e staging continuam offline e com custo zero.
- O adapter direto envia `model_id=eleven_ttv_v3`, valida preview de 100–1000
  caracteres, resposta de 1–3 candidatos, IDs únicos e base64 válido. Retry automático
  ocorre somente em 429 e falhas comprovadamente pré-envio; read timeout e 5xx ambíguos
  não são repetidos cegamente. Logs registram status/request ID sem corpo criativo/chave.
- O grafo agora executa
  `creator_profiles → roster → voice_candidates → review → finalize_voices`. Preview é
  persistido em URI canônica antes do gate; reroll de voz preserva todo o restante do
  plano e respeita o limite dois. Aprovação/finalização exigem candidato do mesmo creator.
- A API aceita somente patches editáveis; o frontend exibe os três áudios, invalida a
  seleção após editar `voice_brief` e bloqueia aprovação incompleta. O endpoint legado
  delega para a mesma regeneração de voz.
- ORM/repositório foram alinhados à migração imutável `0010`. Runner durável injeta
  `PostgresEffectLedger`; design, finalização e TTS usam chaves idempotentes e quotas
  separadas. Custos estimados de design/TTS vêm do YAML e o resumo não duplica resume.

### Falhas investigadas — sintoma → causa → correção

- **Ruff falhou em migrations e `stages.py`:** imports removidos/privados deixaram
  símbolos inconsistentes. **Causa:** a limpeza automática alcançou migrations antigas
  e o stage dependia de `_is_downloadable`. **Correção:** imports históricos 0001–0009
  foram normalizados sem tocar na 0010; `is_downloadable`, `ext_from_*` e `DEFAULT_EXT`
  passaram a ser contratos públicos de `storage.base`, com os mesmos casos de teste.
- **Pytest concluía os testes, mas o processo não saía:** o roteador síncrono pós-TTS e
  `asyncio.to_thread` do R2 mantinham threads no executor default. **Correção:** roteador
  assíncrono e executor explícito compartilhado para storage.
- **Assembly real travou/gerou `BaseSubprocessTransport` após o loop fechar:** pipes de
  FFmpeg eram aguardados em ordem e o timeout matava só o shell, deixando o filho vivo.
  **Correção:** stdout/stderr são drenados concorrentemente; timeout cria e encerra o
  grupo de processos, drena os pipes e aguarda o processo. O warning foi reproduzido
  como erro antes da correção.
- **Contrato live ainda esperava Replicate para creator:** configuração/testes estavam
  presos ao provider antigo e o fixture local não fornecia a nova chave. **Correção:**
  provider e tipo esperado migraram para Voice Design direto; `dev-local` exige
  `ELEVENLABS_API_KEY` e preserva `REPLICATE_API_TOKEN` para vídeo.
- **Factory aceitava configurações parciais/desconhecidas:** fallback legado mascarava
  erros de YAML e políticas não chegavam ao adapter. **Correção:** fallback somente sem
  bloco `voice`; combinações explícitas desconhecidas falham e todos os modelos,
  timeouts, concorrência, retry, candidatos e custos são encaminhados/validados.
- **Respostas malformadas podiam chegar ao fan-out:** `voice_id` vazio e candidato sem
  ID/base64 tinham fallback silencioso. **Correção:** validação estrita antes de
  persistência/finalização e bloqueio de produção sem `voice_ref` em todo creator
  atribuído.
- **Reroll reconstruía creator demais ou aceitava ID de conceito:** o ramo anterior não
  distinguia voz do restante do roster. **Correção:** `target=voices` valida IDs contra
  creators do gate e retorna somente a `voice_candidates`; batches antigos são
  arquivados e deixam de ser selecionáveis.
- **Review antigo reenviava estado server-owned e permitia seleção stale:** URI/provider/
  custo podiam voltar do browser. **Correção:** schema `extra=forbid`, ownership de
  candidato, `gate_id`/version e invalidação ao editar `voice_brief`; frontend serializa
  somente `ReviewCreatorPatch`.
- **Round-trip de migration 0003 → head falhou com `UndefinedColumn`:** o repository
  consultava colunas 0010 antes de a migration de upgrade existir no teste. **Correção:**
  detecção compatível de schema legado durante o upgrade, preservando a 0010 imutável.
- **Seleção de seed creator deixou assignments apontando para IDs removidos:** o roster
  era reduzido sem reescrever referências. **Correção:** assignments agora acompanham o
  creator seed preservado.
- **Testes PostgreSQL falharam no sandbox em `127.0.0.1:5432`:** isolamento de rede, não
  defeito funcional. **Correção operacional:** suíte executada por comando aprovado
  contra PostgreSQL 16 efêmero, sem alterar/afrouxar asserções.
- **Novos testes de factory falharam ao construir compatibilidade Replicate:** faltava
  `REPLICATE_ELEVENLABS_MODEL` no próprio teste. **Correção:** fixture configura o modelo
  obrigatório, mantendo a validação de produção.
- **Teste de custo TTS comparou `0.00012` por igualdade exata:** representação binária
  produziu `0.00011999999999999999`. **Correção:** comparação numérica aproximada, sem
  mudar o cálculo nem o valor contratual.
- **Teste de projeção esperou candidatos em metadata sem batch:** o contrato grava
  `voice_design_meta` apenas quando existe batch de design. **Correção:** expectativa
  passou a verificar metadata vazia/status `legacy`, preservando a semântica real.
- **Teste de validação esperou mutação do gate pendente:** a validação retorna resolução
  nova de propósito e não altera o snapshot canônico antes do resume. **Correção:** o
  teste verifica resolução editada e snapshot original intacto.
- **Suíte funcional verde parou em 98,78% de cobertura:** novos branches defensivos de
  factory, adapter, ledger, DB, grafo, API e frontend não tinham regressões específicas.
  **Correção:** casos de payload/modelo, clientes próprios, reconciliação, transições do
  ledger, IDs/URIs inválidos, reroll/finalização e patches V2 elevaram o gate a 100% sem
  exclusões, skips ou alteração do threshold.
- **Coroutines internas do LangGraph aparecem como warning em cancelamentos deliberados:**
  fechar explicitamente `astream_events` não resolveu e aumentou as ocorrências.
  **Correção:** tentativa descartada; comportamento/checkpoints permanecem corretos e a
  limitação upstream fica registrada, sem silenciar warnings nem alterar testes.
- **Primeiro smoke staging carregou o `DATABASE_URL` remoto do `.env` e o Neon recusou
  por quota de transferência:** a CLI respeitou corretamente o ambiente existente, mas
  esse não era o alvo do aceite offline. **Correção operacional:** comando interrompido e
  repetido com banco/storage locais e tracing explicitamente desabilitado, sem tocar no
  `scripts/dev-local`, containers ou R2 do usuário.
- **Smoke staging offline terminou com custo de US$ 0,16:** apesar de todos os adapters
  serem mock, os tiers do YAML ainda tinham preços live. **RED → GREEN:** regressão passou
  a exigir custo zero em todos os tiers de staging; os valores foram zerados e um novo
  batch concluiu 1/1 com `total_cost_usd=0` e todos os buckets de stage em zero.

### Verificação

- Backend completo com PostgreSQL 16: todos os testes passaram, dois testes live opt-in
  permaneceram legitimamente pulados e a cobertura obrigatória atingiu **100,00%**
  (`8648` statements, zero ausentes).
- Frontend: 13 testes Vitest e build Vite de produção passaram.
- Staging offline isolado: batch 1 determinístico concluiu aprovado, sem rede de provider
  e com custo total zero. O stack `scripts/dev-local` existente não foi reiniciado nem
  alterado; ele não estava visível no Docker context desta sessão para um `/readyz` local.
- O batch live pago não foi disparado: embora as credenciais e o kill switch estejam
  configurados, o `DATABASE_URL` durável foi recusado pelo Neon por quota de transferência
  e o stack `dev-local` do usuário não estava acessível nesta sessão. Não houve bypass do
  ledger/quota nem execução paga não durável.
- Ruff e `git diff --check` passaram. O timeout FFmpeg também passou com
  `PytestUnraisableExceptionWarning` promovido a erro.
