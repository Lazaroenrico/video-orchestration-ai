# PROGRESS — handoff

Estado em **2026-07-01**. Suíte: **276 passando, 2 skips** (testes `--live` opt-in,
pulados sem `JUDGE_GATEWAY_URL`) + 2 warnings conhecidos/benignos (LangSmith
deprecation em import; LangGraph resume parcial — ver falha #5).
Rodar: `rtk proxy python -m pytest`.

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
    `/media/{run_id}/items/{item_id}/…` (provenance em `meta["source_uri"]`); no-op para
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
- [x] `nodes/stages.py` + `nodes/base.py` — os 10 stages como nodes
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

**Env vars para MVP:** `AI_GATEWAY_API_KEY`, `TOPAZ_API_KEY`, `ELEVENLABS_API_KEY`,
`REPLICATE_API_TOKEN`. Tabela completa em **D20**.

**Smoke test pós-implementação:**
```bash
# CI (sem chaves — deve passar 100%)
rtk proxy python -m pytest

# Instancia os adapters reais
AI_GATEWAY_API_KEY=<chave> TOPAZ_API_KEY=<chave> ELEVENLABS_API_KEY=<chave> REPLICATE_API_TOKEN=<chave> \
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
   - [x] Creator: GPT Image 2 + Topaz + ElevenLabs (`adapters/creator_real.py`) — `creator: creator_real`
         + `OPENAI_API_KEY`/`TOPAZ_API_KEY`/`ELEVENLABS_API_KEY`.
   - [x] Vídeo Replicate (`adapters/replicate_video.py`, D14) — `video: replicate` + `REPLICATE_API_TOKEN`.
   - **Pendente p/ rodar real:** (a) expor as chaves no ambiente; (b) contratos HTTP de
     Topaz/ElevenLabs são assumidos (docstrings) — validar contra APIs reais com as chaves;
     (c) Steps 8/9 seguem mock (sem API única). Ver MVP acima (D20).
2. **Step 9 (distribuição) real** (cloud phones/proxies/scheduler) — hoje mock.
3. **Topologia data-driven**: mover nodes/edges para o `pipeline.yaml` (hoje fixa no builder).
4. **LangSmith**: setar `LANGSMITH_TRACING=true`/`LANGSMITH_API_KEY` p/ tracing; opcional
   subir o eval do Judge via `langsmith.evaluate` (hoje o evaluator roda local/offline).
5. [x] **CLI do loop**: `runner.run_cycles` + comando `orchestrator loop --cycles N
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
