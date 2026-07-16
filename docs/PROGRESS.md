# PROGRESS — handoff

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
  quebrar no meio de um run pago.

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

**Pendente:** Fase 4 (signed URLs sob demanda na UI). `get_signed_url` está implementado e
testado, e signed URL nunca é persistida — mas a consequência da ADR "a UI passa a receber
signed URLs sob demanda" ainda não vale: `_normalize_artifact`/`_normalize_creator` são
síncronos e `get_signed_url` é async, então falta um pre-pass async sobre o payload. Só
importa quando `storage.backend: r2` for ligado, o que depende do bucket ser provisionado.

**Verificação:** `rtk proxy .venv/bin/python -m pytest` → **857 passed, 2 skipped**,
cobertura 100% (era 772). Dirigido fora da suíte: run mock de batch 4 gravou 18 artifacts
reais no DB com key canônica, content_type, size_bytes e sha256 — 8 `intermediate` com
`expires_at` em +2 dias e 10 `keep` sem expiração, com a última take de cada item retida.
Ao vivo não rodado: exige bucket R2 provisionado (`R2_*`), que ainda não existe.

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
