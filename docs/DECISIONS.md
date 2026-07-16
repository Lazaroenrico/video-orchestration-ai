# Decisões (ADR leve)

Registro de todas as decisões tomadas, com contexto → decisão → consequência.
Datas absolutas. Apendar novas decisões ao final.

## 2026-06-27

### D1 — Escopo do v1: só o motor de orquestração
- **Contexto:** `Context.md` descreve uma pipeline de 10 passos com muitas integrações
  externas (Claude, GPT Image 2, Topaz, ElevenLabs, plataformas de vídeo, distribuição).
- **Decisão:** v1 entrega só o *motor* genérico (grafo, fan-out, retry/QC gate, estado,
  CLI). Os 10 stages entram como nodes mock.
- **Consequência:** roda ponta a ponta sem custo; integrações reais ligam depois sem
  mexer no grafo.

### D2 — Stack: Python 3.11+
- **Decisão:** Python pelo ecossistema de IA e async/batch.
- **Consequência:** SDKs reais (anthropic, elevenlabs, replicate, fal) plugáveis depois.

### D3 — Tudo mock/dry-run primeiro
- **Decisão:** todos os adapters começam mock, determinísticos.
- **Consequência:** arquitetura validável sem gastar créditos; testes reproduzíveis.

### D4 — Provedor de vídeo abstraído (decidir depois)
- **Contexto:** Replicate vs fal.ai vs AtlasCloud em aberto.
- **Decisão:** abstrair atrás de um Protocol (`VideoPort`); escolher o provedor depois.
- **Consequência:** `config/providers.yaml` mapeia papel→adapter; trocar é 1 linha.

### D5 — TDD estrito + integridade dos testes
- **Decisão:** red→green→refactor em tudo; **nunca** afrouxar teste para passar — em
  falha, achar a causa raiz.
- **Consequência:** suíte é a especificação executável; falhas viram aprendizado em
  `PROGRESS.md`.

### D6 — Frameworks: LangGraph + LangChain + LangSmith
- **Decisão:** LangGraph como motor (StateGraph, Send, conditional edges, checkpointer);
  LangChain para adapters como Runnables; LangSmith para tracing + eval do Judge.
- **Consequência:** menos engine custom; resumibilidade e observabilidade "de graça".

### D7 — LLM Judge via API Gateway config-driven
- **Contexto:** o usuário usará um API Gateway; contrato exato indefinido.
- **Decisão:** `adapters/judge.py` monta a request por `config/judge.yaml`
  (url/method/headers/body_template) e extrai score/verdict por caminho pontilhado.
- **Consequência:** trocar o gateway/contrato não toca código.

### D8 — Determinismo do Judge: cassette/replay + `--live`
- **Decisão:** no CI o judge roda em replay de um cassette golden (sem rede); `--live`
  chama o gateway real e regrava o cassette.
- **Consequência:** eval determinístico e offline por padrão; revalidação opt-in.
- **Nota:** o cassette é chaveado por **id lógico do subject** (não pelo hash dos bytes
  da request) — torna o golden robusto a mudanças de formatação do template.

### D9 — Checkpointer assíncrono compatível com SQLite
- **Contexto:** os nodes são async e o grafo roda via `ainvoke`; o `SqliteSaver` sync
  lança `NotImplementedError` em métodos async; `aiosqlite.connect` trava neste ambiente.
- **Decisão:** usar uma fachada async (`AsyncSqliteCompatSaver`) sobre `SqliteSaver`,
  com um `JsonPlusSerializer` que registra os tipos pydantic do estado
  (`Item`/`Artifact`/`QCResult`) em `allowed_msgpack_modules`.
- **Consequência:** resumibilidade async funciona e o aviso de "tipo não registrado"
  (que seria bloqueado em versões futuras do LangGraph) some.

### D10 — Topologia do grafo fixa no builder (v1)
- **Contexto:** o plano falava em montar nodes/edges "a partir do yaml".
- **Decisão:** no v1 a topologia é fixa em `graph/builder.py`; o `pipeline.yaml` ajusta
  só os knobs (batch, concorrência, QC, tiers/custo). Os 10 stages ficam agrupados em
  `nodes/stages.py` (um módulo) em vez de 10 arquivos `n01..n10`.
- **Consequência:** menos boilerplate; tornar a topologia data-driven é evolução futura.

### D11 — Tier routing LTX-only e custo acumulado
- **Decisão:** tentativas de QC permanecem no primeiro tier configurado (`ltx` no
  `pipeline.yaml` atual). `attempts` controla apenas o orçamento do loop de QC; cada
  geração acumula custo.
- **Consequência:** o loop de regeneração termina pelo teto de tentativas sem disparar
  `kling`/`seedance` automaticamente. Tiers premium continuam no config para uso futuro
  ou chamada explícita, mas não participam do roteamento automático.

### D12 — Orquestração da construção com subagentes (Opus coordena, Sonnet executa)
- **Contexto:** pedido de usar "um orquestrador principal Opus + sub-agents Sonnet por
  dificuldade".
- **Decisão:** o agente Opus coordena e despacha **subagentes Sonnet** (Claude Code
  Agent tool) para tarefas específicas de dificuldade média, com **escopos de arquivo
  disjuntos** (rodam em paralelo sem conflito). Integração, verificação da suíte completa
  e documentação ficam com o Opus.
- **Consequência:** como o workspace **não é git**, não há worktrees isoladas; a
  segurança do paralelismo vem do particionamento estrito de arquivos por subagente.

### D13 — Close-the-loop de feedback (Step 10 → Step 1)
- **Decisão:** `feedback_store.py` persiste o agregado de cada run num JSON chaveado por
  `run_id`, com índice incremental `_idx` para definir "mais recente" de forma
  **determinística** (timestamps de FS não são confiáveis em CI/containers). O `runner`
  carrega o feedback do ciclo anterior e injeta `prior_winning_styles` no estado inicial;
  a CLI expõe `--feedback-store`.
- **Consequência:** o loop fecha de fato; usar os `winning_styles` como **viés na geração
  de conceitos** fica como próximo passo (exige tornar `generate_concepts` ciente do viés).

### D14 — Adapter de vídeo real (Replicate) com httpx async injetável
- **Decisão:** `ReplicateVideoAdapter` implementa `VideoPort` via `httpx.AsyncClient`
  injetável (testável com `MockTransport`, sem rede), registrado em `registry.py` como
  `"replicate"`; lê `REPLICATE_API_TOKEN` do ambiente. Custo por tier idêntico ao Context.
- **Consequência:** prova o caminho de plugar um provedor real sem tocar o grafo; trocar
  `video: mock` → `video: replicate` em `providers.yaml` basta.

### D15 — Viés de geração de conceitos pelos vencedores (completa o close-the-loop)
- **Decisão:** `mock.generate_concepts` ganhou `bias` opcional (retrocompatível): uma
  fração (~0.6) dos conceitos é puxada para os `winning_styles` do ciclo anterior,
  mantendo determinismo e spread. `node_concepts` passa `prior_winning_styles`; `LLMPort`
  atualizado para refletir o param.
- **Consequência:** o dado do back-end (o que converteu) chega ao front-end e muda o que é
  produzido — fiel ao Step 10 do Context ("o sistema fica mais afiado a cada ciclo").

## 2026-06-29

### D16 — CLI do loop multi-ciclo (`run_cycles` + `orchestrator loop`)
- **Contexto:** o close-the-loop de um ciclo já existia (D13/D15); faltava encadear N
  ciclos para o sistema "se afiar" de fato a cada iteração (item 5 do v2).
- **Decisão:** `runner.run_cycles` roda `run_pipeline` em sequência, cada ciclo com
  `thread_id` próprio (`{prefix}-c{i}`, checkpoint separado) mas **compartilhando o mesmo
  `feedback_store`** — como `run_pipeline` já lê o feedback mais recente (vira viés) e o
  node de feedback grava no fim, encadear é só iterar. `feedback_store` é **obrigatório**
  (sem ele não há o que encadear → `ValueError`); `cycles < 1` também é rejeitado. A CLI
  expõe `orchestrator loop --cycles N --feedback-store ...` e imprime um relatório por ciclo.
- **Consequência:** zero lógica nova de estado/grafo — reusa toda a máquina existente; o
  efeito de aprendizado é observável (a cada ciclo o mix de hooks puxa para os vencedores
  do anterior). Cada ciclo continua sendo um run inspecionável via `status`/`list`/`resume`.

### D17 — Resolução de adapter por papel (`CompositeAdapter`)
- **Contexto:** o `registry` só resolvia o papel `video` e usava esse adapter único para
  TODOS os métodos. Para misturar adapters reais e mock por papel (ex.: Claude no LLM,
  mock no resto) era preciso rotear por papel.
- **Decisão:** `build_adapter_from_providers` monta um `CompositeAdapter` que delega cada
  método ao adapter do papel correspondente (`llm`/`creator`/`video`/`qc`/`assembly`/
  `distribution`). Papéis ausentes em `providers.yaml` caem em `mock`; cada nome distinto é
  instanciado **uma vez** (compartilhado entre papéis — preserva o determinismo do mock e
  **não constrói adapters reais sem necessidade**, então a suíte roda sem nenhuma chave).
  `providers.yaml` passou a usar o papel **`creator`** (que dobra image+voice, já que o
  `CreatorPort.build_creator` é um método único que compõe GPT Image 2 + Topaz + ElevenLabs).
- **Consequência:** ligar um papel real é trocar 1 linha (para LLM live, `llm: mock` →
  `llm: vercel_gateway_llm`) sem tocar o grafo; o resto segue mock. Testes:
  `test_registry_composite.py` (2).

### D18 — Adapters reais (Claude LLM + Creator), só as ligações
- **Contexto:** pedido de "rodar com as APIs conectadas"; Steps 8 (montagem) e 9
  (distribuição) não têm API única e seguem mock. O caminho live de LLM será via gateway.
- **Decisão:** construídos via subagentes Sonnet (Opus coordena — D12), em escopos de
  arquivo disjuntos, com TDD **offline** (cliente/transport injetável, sem rede/sem chaves):
  - `adapters/anthropic_llm.py` (`LLMPort`) — SDK oficial `anthropic`, modelo
    `claude-opus-4-8`, `thinking={"type":"adaptive"}`, **Structured Outputs**
    (`output_config.format.schema`) para os conceitos; sem `temperature`/`top_p`/`top_k`/
    `budget_tokens` (400 no Opus 4.8); guarda `stop_reason=="refusal"`. (`test_anthropic_llm.py`, 20)
  - `adapters/{openai_image,topaz_upscale,elevenlabs_voice}.py` + `creator_real.py`
    (`CreatorPort`) — httpx async injetável (padrão do `ReplicateVideoAdapter`), contratos
    HTTP documentados em docstring. (`test_creator_real.py`, 13)
  - Registrados em `registry.py` como `anthropic` e `creator_real`. Dep `anthropic>=0.40`.
- **Correção pós-subagente:** o structured output usava a chave errada
  (`json_schema`); o SDK 0.112 exige `schema` (`JSONOutputFormatParam`) — corrigido.
- **Consequência:** as ligações existem e testam offline; para rodar real basta a chave no
  ambiente + flip em `providers.yaml`. Video real (Replicate, D14) já existia.

### D19 — Vercel AI Gateway só para o LLMPort nesta rodada
- **Contexto:** o LLMPort (Steps 1 e 2) já usa `AnthropicLLMAdapter`; era preciso ativar
  Vercel AI Gateway sem mexer na topologia do grafo nem expandir o escopo para creator,
  video ou judge.
- **Decisão:** adicionar o provider `vercel_gateway_llm`, que reaproveita
  `AnthropicLLMAdapter` com `AsyncAnthropic(api_key=..., base_url=...)` apontando para o
  Vercel AI Gateway. A autenticação aceita `AI_GATEWAY_API_KEY` ou `VERCEL_OIDC_TOKEN`.
  **Todo tráfego LLM real do projeto deve passar pelo Vercel AI Gateway**, inclusive
  quando o modelo subjacente for da família Anthropic. O provider direto `anthropic`
  permanece só por retrocompatibilidade/legado; `mock` também permanece. Judge segue como está.
- **Consequência:** os Steps 1/2 podem rodar via gateway trocando só `llm:` em
  `config/providers.yaml`; `anthropic` direto deixa de ser o caminho live normal.
  Creator, video, QC/judge e resto do runtime ficam fora do escopo desta rodada.

### D20 — MVP: Vercel AI Gateway para Anthropic e OpenAI; ativar todos os adapters reais
- **Contexto:** para o MVP funcional a decisão é usar o Vercel AI Gateway como ponto
  único de entrada para todos os modelos suportados: Claude (Anthropic) e GPT Image 2
  (OpenAI). Topaz e ElevenLabs não têm suporte no gateway e continuam com chaves diretas.
  Replicate (vídeo) também continua direto.
- **Decisão:** implementar em 6 tasks cirúrgicas sem tocar o grafo LangGraph:
  1. `adapters/openai_image.py` — adicionar `build_openai_image_vercel_adapter` que aponta
     para `https://ai-gateway.vercel.sh/openai/v1` usando `AI_GATEWAY_API_KEY` (mesmo
     token do LLM).
  2. `adapters/creator_real.py` — adicionar `build_real_creator_vercel_adapter` que compõe
     `OpenAIImageAdapter` (via gateway) + `TopazUpscaleAdapter` (direto) +
     `ElevenLabsVoiceAdapter` (direto).
  3. `registry.py` — registrar `"creator_real_vercel"`. Sub-adapters (elevenlabs, topaz,
     openai_image) são internos ao creator e não recebem entrada própria no registry.
  4. `config/providers.yaml` — ativar `llm: vercel_gateway_llm`, `creator: creator_real_vercel`,
     `video: replicate`; manter `qc/assembly/distribution: mock`.
  5. `config/judge.yaml` — atualizar header Authorization para aceitar `AI_GATEWAY_API_KEY`.
     O judge ao vivo requer um proxy/Vercel Function intermediária (fora do escopo Python)
     que adapte a resposta Claude para `{"output": {"score": float, "verdict": str}}`.
  6. `.env.example` — atualizar status de `TOPAZ_API_KEY` e `ELEVENLABS_API_KEY` para live
     no caminho direto/legado `creator_real_vercel`.
- **Env vars necessárias para este MVP original:**

  | Variável | Para quê | Obrigatória |
  |---|---|---|
  | `AI_GATEWAY_API_KEY` | Token único Vercel Gateway (LLM + imagem) | Sim |
  | `TOPAZ_API_KEY` | Upscale direto Topaz Labs | Sim |
  | `ELEVENLABS_API_KEY` | Voz direto ElevenLabs (`creator_real_vercel`, legado) | Sim nesse caminho |
  | `REPLICATE_API_TOKEN` | Vídeo via Replicate | Sim |
  | `REPLICATE_ELEVENLABS_MODEL` | Voz ElevenLabs hospedada no Replicate (`creator_real_replicate`, atual) | Sim no caminho atual |
  | `AI_GATEWAY_BASE_URL` | Override da URL base do gateway | Não (default: `https://ai-gateway.vercel.sh`) |
  | `AI_GATEWAY_LLM_MODEL` | Override do modelo Claude | Não (default: `anthropic/claude-opus-4.8`) |
  | `JUDGE_GATEWAY_URL` | URL do proxy wrapper do judge | Só para judge ao vivo |

- **O que NÃO muda:** grafo LangGraph, MockAdapter, runner, CLI, cassette do judge,
  nenhum teste existente. Steps 8/9 seguem mock (sem API única).
- **Consequência:** com as 4 chaves no ambiente e o flip do providers.yaml, a pipeline
  roda ponta a ponta real (Steps 1-7 reais, 8-9 mock). A suíte de testes continua 100%
  offline e determinística — nenhum teste existente muda.

## 2026-06-30

### D21 — Tracing LangSmith por node e adapter, com gate runtime
- **Contexto:** só o root run recebia `run_trace_config`; não havia spans explícitos nos
  10 passos, nos adapters, nem no Anthropic SDK direto. Além disso, o CLI carrega `.env`
  depois de importar módulos, então decidir tracing no import impediria ativar spans por
  `.env`.
- **Decisão:** centralizar tracing em `orchestrator.tracing` com `@traced`, sanitizer de
  inputs/outputs/metadata, `wrap_anthropic_client` e gate runtime por
  `LANGSMITH_TRACING`. Os nodes e adapters ganham spans nomeados offline-testáveis via
  marcadores `__trace_*`. `config`, `self`, clients, headers, tokens, prompts, scripts,
  URLs/data URIs e blobs base64 não são serializados nos spans. `offer` entra no root
  trace só como hash curto.
- **Consequência:** com tracing off, mocks e testes continuam determinísticos/offline; com
  `LANGSMITH_TRACING=true`, o LangSmith enxerga root run, nodes da pipeline, roteamento
  por papel no `CompositeAdapter`, adapters concretos e wrapper do client Anthropic.

### D22 — Dashboard human-on-the-loop com timeline de artefatos
- **Contexto:** a UI web já inicia runs, consome SSE, mostra steps do LangGraph,
  tokens do LLM e aprova creators via interrupt. Porém a visualização ainda é centrada
  em status de nodes e só mostra o item completo no fim de `process_item`, ocultando o
  que acontece durante script, talking-head, product demo, QC, montagem e distribuição.
- **Decisão:** o dashboard vira um cockpit operacional human-on-the-loop. A UI deve
  streamar updates por item ao longo do fluxo, exibindo conceito, script gerado, mídia
  produzida, QC, vídeo final e distribuição. No v1 desta decisão, o único checkpoint
  bloqueante continua sendo o aceite de creators; os demais outputs são revisáveis e
  auditáveis, mas não pausam o grafo.
- **Contrato de UI/eventos:** o backend web deve emitir `item_update` a partir dos
  `node_end` dos stages per-item, acumulando snapshots por `item_id`. Creators devem ser
  normalizados com campos explícitos (`image_uri`, `voice_ref`, `voice_preview_uri`) e
  aliases legados (`image`, `voice`) enquanto a UI migra. Artefatos devem carregar
  `kind`, `uri`, `media_type` e `renderable`.
- **Política de mídia:** a UI só renderiza preview/player quando a URI for navegável
  (`http(s)`, path local servido, ou `data:` compatível). URIs `mock://...`, `voice_id`
  e refs técnicas aparecem como referência rastreável, não como mídia quebrada.
- **Creator store:** `creator_store` continua sendo histórico pós-decisão humana, mas
  passa a persistir os campos normalizados de creator de forma retrocompatível. Ele não
  é a fonte do streaming ao vivo; a fonte ao vivo são eventos SSE do run.
- **Segurança de renderização:** conteúdo gerado por LLM/adapters deve entrar na UI por
  `textContent`/criação DOM segura, não por `innerHTML` com interpolação direta.
- **Consequência:** o operador consegue acompanhar o processo enquanto ele acontece:
  ver o rosto enviado, identificar a voz ou ouvir preview quando existir, ler o script,
  inspecionar clips e QC, e aprovar creators antes do fan-out. Gates adicionais para
  script/mídia/final ficam como evolução futura, sem alterar esta decisão.

### D23 — Vídeo Replicate LTX 2.3 Fast sem áudio
- **Contexto:** o `ReplicateVideoAdapter` antigo usava um contrato HTTP fictício
  (`/predictions`, `model`, `output`) enquanto os adapters reais de upscale/voz já usam
  o SDK oficial `replicate.async_run`. O objetivo desta fatia é gerar vídeo real sem
  áudio; voiceover e concatenação entram depois.
- **Decisão:** `ReplicateVideoAdapter` passa a usar `replicate.async_run(ref, input=...)`
  com runner injetável e `with_transport_retry`. O tier `ltx` aponta para
  `lightricks/ltx-2.3-fast`, recebe `prompt`, `duration`, `image` opcional do creator,
  `resolution`, `aspect_ratio`, `fps`, `camera_motion` e força `generate_audio: false`.
  O fan-out carrega `creator_image_uri` a partir de `image_source_uri`/`upscaled_base`,
  e os nodes de vídeo compõem um prompt com script + conceito + `video_prompt`.
- **Fallback:** `kling` e `seedance` ainda não têm refs reais confirmadas no Replicate;
  se forem chamados explicitamente, o adapter delega ao `MockAdapter` e marca
  `fallback_reason: replicate_model_not_configured`. O QC automático permanece em LTX.
- **Consequência:** `config/providers.yaml` ativa `video: replicate` e o grafo não muda.
  A suíte segue offline por runner injetável/mock fallback; áudio do script permanece
  fora do escopo desta decisão.

### D24 — TTS live somente ElevenLabs hospedado no Replicate
- **Contexto:** o perfil live `creator_real_replicate` usava Replicate para upscale e
  voz, mas o sub-adapter de voz ainda apontava para `suno-ai/bark`. A regra de produto é
  não usar outro TTS: somente ElevenLabs, com o modelo hospedado no Replicate.
- **Decisão:** `ReplicateVoiceAdapter` passa a ser um wrapper de ElevenLabs via
  Replicate. O ref do modelo é obrigatório por `REPLICATE_ELEVENLABS_MODEL` (ou
  `model=` em teste), e o schema de input é configurável por envs (`*_TEXT_FIELD`,
  `*_VOICE_FIELD`, `*_MODEL_ID_FIELD`, `*_INPUT_JSON`, voice ids por preset).
  `creator_real_replicate` mantém GPT Image 2 via Vercel Gateway e Replicate para
  upscale, mas a voz live deixa de usar Bark/Suno.
- **Consequência:** falta configurar o ref real `owner/model:version` do modelo
  ElevenLabs no `.env`. Sem ele, o perfil live falha cedo em vez de cair silenciosamente
  para outro TTS. O caminho `ElevenLabsVoiceAdapter` direto permanece só para adapters
  legados como `creator_real_vercel`.

## 2026-07-02

### D25 — Remover distribuição do escopo do motor
- **Contexto:** o produto não deve mais orquestrar postagem/agendamento. O motor deve
  parar quando o vídeo final estiver montado, deixando qualquer publicação fora deste
  repo.
- **Decisão:** remover o Step 9 de distribuição do grafo, do estado, dos adapters, da UI,
  dos providers e dos testes. O subgrafo per-item agora termina em `assembly` ou `drop`.
  Item aprovado/finalizado passa a significar `assembled is not None and not dropped`.
- **Consequência:** `DistributionPort`, `MockAdapter.distribute`, role `distribution`,
  node `node_distribution`, campo `Item.distributed` e card de UI "Distribuição" deixam
  de existir. Feedback e summaries contam aprovados por vídeo montado.

### D26 — Perfil live sem mock e assembly final via Seedance 2.0
- **Contexto:** o perfil `config/` ainda era híbrido: LLM/creator/vídeo reais, mas QC e
  assembly em `mock`. Isso impedia validar que o vídeo final era realmente gerado pelo
  modelo desejado.
- **Decisão:** `config/` passa a ser o perfil live sem mock nos papéis runtime:
  `llm: vercel_gateway_llm`, `creator: creator_real_replicate`, `video: replicate`,
  `qc: integrity_qc` e `assembly: vercel_seedance_assembly`. O dry-run fica em
  `config-mock/`. O QC live é uma checagem de integridade de artefatos (sem VLM): bloqueia
  clips mock, fallback e URIs não-vídeo. A montagem final usa Seedance 2.0 via Vercel AI
  Gateway (`bytedance/seedance-2.0`) por um bridge Node com AI SDK
  `experimental_generateVideo`.
- **Consequência:** o grafo segue igual, mas os contratos de `QCPort` e `AssemblyPort`
  passam a receber o `Item` completo. `MockAdapter` mantém compatibilidade com chamadas
  antigas por `item_id`. `ReplicateVideoAdapter` mantém fallback mock como default para
  testes/compatibilidade, porém `config/pipeline.yaml` define
  `video.allow_mock_fallback=false`, fazendo tiers sem adapter real falharem
  explicitamente no live em vez de mascarar o problema.

## 2026-07-06

### D27 — UI "Kinetic Command": SPA React em `front/` substitui o dashboard estático
- **Contexto:** o dashboard era um único `web/static/index.html` (dark, página única)
  servido pelo FastAPI. A UI/UX foi redesenhada no Stitch (design system "Kinetic
  Command", tema claro, 12 telas) e o HTML monolítico não comportava esse escopo.
- **Decisão:** implementar a UI como **SPA Vite + React + TypeScript + Tailwind** em
  `front/` (fonte em `front/src/`), buildada para `front/dist/` e servida pelo FastAPI.
  `web/static/index.html` foi removido. As 12 telas são ligadas a dados reais via `/api/*`
  + SSE onde há backend; telas sem backend (Analytics/Settings/Publishing) ficam fiéis ao
  design com dados agregados/estáticos. Novo `GET /api/integrations` expõe o mapa
  stage→adapter de `providers.yaml`.
- **Consequência:** `GET /` serve `front/dist/index.html` (com fallback HTML instruindo
  `npm run build` quando não buildado — mantém o CI sem Node verde); `/assets` é montado
  para os bundles do Vite; um catch-all `GET /{path}` serve o index para rotas client-side
  **sem** sombrear `/api|/media|/videos|/assets`. `front/dist` e `front/node_modules` são
  gitignored — a SPA precisa ser buildada antes de `orchestrator serve`. Os testes que
  faziam *grep* no HTML/JS do dashboard antigo foram removidos (cobriam código deletado);
  `tests/test_web_spa.py` cobre o novo contrato de serviço; a lógica de backend
  (`_build_item_update`, normalizadores, CRUD de `/api/prompts`) segue intacta.

### D28 — Concepts/scripts antes do creator com gate humano de edição
- **Contexto:** o script era gerado dentro do subgrafo por item, depois do roster de
  creators. Isso impedia revisar, editar ou descartar conceitos antes de gastar creator,
  voz e vídeo.
- **Decisão:** a topologia passa a ser `concepts -> scripts -> concept_review -> roster
  -> approval -> fan-out -> process_item -> feedback`. `node_scripts` escreve um script
  por conceito em nível de batch usando `creator_ref="creator"` como placeholder
  genérico. `node_concept_review` pausa quando `run.edit_concepts=true` e substitui a
  lista por conceitos editados/incluídos. No fan-out, `concept["script"]` é movido para
  `Item.script` e removido de `Item.concept`.
- **Contrato web/UI:** `RunRequest.edit_concepts` tem default `True`; o backend emite
  `awaiting_concept_edit` via SSE e aceita `POST /api/approve/{run_id}/concepts` com a
  lista final. A tela React `/scripts` renderiza editor de concept+script durante a fase
  `editing`, com checkbox de inclusão e submit "Save & Continue".
- **Consequência:** o creator só roda depois da revisão humana de copy. Scripts deixam
  de depender de uma persona específica; a persona real é atribuída depois no fan-out.

## 2026-07-14

### D29 — Migração incremental para agents executions sobre a camada de tools
- **Contexto:** o motor atual usa LangGraph como runtime de orquestração, com fan-out,
  conditional edges, interrupts humanos, checkpointer e resume. A base já foi preparada
  com a camada `orchestrator.tools`: nodes chamam tools tipadas, as tools validam shape
  e delegam para o `CompositeAdapter`, que resolve adapter por papel. Migrar tudo de uma
  vez para um runtime agentic colocaria em risco resumibilidade, determinismo, UI/SSE,
  testes offline e o contrato de dry-run.
- **Decisão:** a migração para agents executions será incremental. LangGraph permanece
  como orquestrador canônico do fluxo no primeiro ciclo da migração. A camada `tools/`
  vira a fronteira estável para agents: qualquer agent futuro deve chamar tools tipadas,
  não adapters diretamente. O `CompositeAdapter` continua sendo a fonte de roteamento
  provider/adapters por papel. O próximo passo arquitetural é introduzir um catálogo de
  agents/models por stage/tool, sem alterar a topologia do grafo nem quebrar `config-mock`.
- **Estratégia:** primeiro consolidar `TOOL_REGISTRY` como contrato público interno
  (`name`, `role`, `stage`, descrição, `function_path`, capabilities e modelo/agente alvo
  quando existir). O catálogo configurável vive em `agents.yaml`; quando ausente, todos os
  stages ficam em `executor: tool` por compatibilidade. Depois criar uma camada fina de
  agent execution que possa ser ativada por configuração para stages específicos,
  começando por LLM-only (`concepts` e `scripts`). Stages de mídia (`creator`, `video`,
  `assembly`, `upscale`) continuam via adapters até haver contrato agentic testado para
  cada um.
- **Consequência:** a migração pode ser feita stage por stage, mantendo testes
  determinísticos, cassettes offline, dry-run sem custo, tracing por node/tool/adapter e
  compatibilidade com CLI/web. O runtime agentic não pode bypassar validação de tools,
  checkpointer LangGraph, gates humanos, persistência de mídia ou regras de shape dos
  adapters. A primeira execução runtime fica restrita a `concepts` e `scripts`; stages de
  mídia seguem bloqueados para `agent` até haver contrato específico.
- **Fora de escopo:** substituir LangGraph por completo, remover `CompositeAdapter`,
  mudar contratos públicos da CLI/web, ou acionar agents live por padrão. Essas mudanças
  exigem ADR própria depois que a camada agentic incremental estiver validada.

### D30 — R2 + DB relacional como arquitetura canônica de mídia
- **Contexto:** `media_store.py` já baixa URLs voláteis de providers para disco local
  (`ORCH_MEDIA`/`ORCH_VIDEOS`) e reescreve URIs para `/media/...` e `/videos/...`. Isso
  preserva o dry-run e a UI local, mas não é uma fonte canônica adequada para produção:
  bytes grandes de imagem, áudio e vídeo precisam sair do DB e do filesystem local,
  enquanto metadados, estado, auditoria, custo, retenção e relações precisam de um DB
  relacional.
- **Decisão:** em produção, os bytes de mídia serão persistidos em Cloudflare R2 via API
  S3-compatible. O DB relacional será a fonte da verdade para artifacts e ponteiros,
  começando SQLite-first para preservar o modo offline atual. O R2 guarda apenas bytes; o
  DB guarda `storage_key`, tipo, tamanho, hash, proveniência, retenção e relações com
  run/item/creator. URLs assinadas serão geradas sob demanda para UI, downloads e
  handoff para providers externos; elas não serão persistidas como valor canônico.
- **Abstração:** a camada atual de persistência de mídia deve evoluir para um contrato
  contínuo com duas implementações: `LocalMediaStorage` para mock/dry-run/dev/testes e
  `R2MediaStorage` para live. LangGraph, tools e adapters continuam recebendo `Artifact`
  e metadados, sem conhecer detalhes de R2. `config-mock` continua sem rede e sem custo.
- **Retenção:** creator assets, clips aprovados e vídeos finais montados são retidos.
  Clips reprovados são short-lived e expiram após **3 dias**. Tentativas intermediárias
  são short-lived e expiram após **2 dias**. A limpeza deve operar pelos metadados do DB,
  não por varredura cega do bucket.
- **Consequência:** a arquitetura separa corretamente bytes pesados de estado
  transacional, mantém compatibilidade com a D29 (mídia ainda adapter-driven), reduz
  dependência de URLs temporárias dos providers e prepara o caminho para signed URLs,
  auditoria e políticas de expiração sem quebrar o fluxo offline existente.

## 2026-07-15

### D31 — Execução agentic real via adapter LLM gateway-nativo (Fase 7 do D29)
- **Contexto:** a Fase 7 do D29 (ADR `docs/ADR-D31-agentic-execution.md`) introduz o loop
  agentic *critique → refine* bounded em `concepts`/`scripts`, com o brain no adapter LLM
  via `AgentPort.run_stage_agent` e `revision` como canal genérico de refino. O backend
  real precisava alcançar o modelo **pelo AI gateway**, sem amarrar o motor ao SDK
  `anthropic`.
- **Decisão:** o adapter LLM real default (`vercel_gateway_llm`) passa a ser
  **gateway-nativo**: `GatewayLLMAdapter` fala com o Vercel AI Gateway por `httpx` puro
  contra `POST {base}/chat/completions` (OpenAI-compatible, Structured Outputs via
  `response_format: json_schema`), implementando `LLMPort` + `AgentPort`. Sem importar o
  SDK `anthropic`. O `AnthropicLLMAdapter` (SDK direto ou SDK apontado ao gateway) fica
  registrado como **legado opt-in** (`anthropic`, `anthropic_sdk_gateway`).
- **Fronteiras (herdadas do D29):** o agent só toca o domínio via `run_tool` (typed tool
  validada); o loop é bounded a um refinamento; agent execution só em `concepts`/`scripts`;
  a crítica nunca levanta (falha → aprova o rascunho já validado). O mock permanece
  determinístico/offline (`config-mock` segue `executor: tool`, custo zero).
- **Consequência:** o backend LLM live roda 100% pelo AI gateway, desacoplado do SDK
  Anthropic, cobreto offline via `httpx.MockTransport` (cliente injetável). Streaming de
  token do LLM fica fora desta fase (não-streaming). `config/providers.yaml` não muda —
  `llm: vercel_gateway_llm` passa a resolver o adapter gateway-nativo.

### D32 — Loop de tool-calling real (substitui o wrapper critique→refine bounded)
- **Contexto:** o D31 entregou um wrapper agentic *fixo* de 2 passos (draft → critique →
  refine ×1): o modelo só devolvia uma diretiva de texto ou `APPROVE`, sem receber schemas
  de tools nem escolher tools. O D29 marcou "tool-calling real / live-by-default agents"
  como fora de escopo, exigindo ADR próprio — este.
- **Decisão:** `AgentPort.run_stage_agent` passa a rodar um **loop de tool-calling real**
  (ReAct bounded). O modelo recebe os schemas das tools permitidas (`tool_call_schemas`
  no `tools/registry.py`), decide **quais** chamar e **com que args**, e itera multi-pass
  até parar (sem tool call) ou estourar `agent.max_steps` (novo knob em `pipeline.yaml`,
  default `_agent_loop.DEFAULT_MAX_STEPS=4`). O loop compartilhado vive em
  `adapters/_agent_loop.py` (budget, allowlist, fronteira D29 e safety-net num só lugar);
  os brains provider-específicos (`_GatewayAgentBrain` OpenAI function-calling,
  `_AnthropicAgentBrain` `tool_use` do SDK, `_MockAgentBrain` determinístico) fazem a
  ponte com o modelo.
- **Fronteiras / segurança:** o `StageToolRunner` vira `run_tool(tool_name, **inputs)` — o
  agent **nomeia** a tool; o stage executor valida o nome contra `allowed_tools` (D29) e
  mantém offer/n/seed/etc. **server-authoritative**, filtrando os args do modelo apenas
  para os params declarados no schema da tool (hoje só `revision`). Safety-net: se o modelo
  nunca chamar uma tool válida, a tool primária roda uma vez — o stage sempre produz output
  de domínio válido. Agent execution segue restrito a `concepts`/`scripts` (`_AGENT_STAGES`).
- **Consequência:** o motor deixa de ser um wrapper fixo e passa a ter um agente que
  escolhe/itera tools de verdade, coberto offline (MockTransport / fake SDK client /
  brain determinístico), sem custo. Multi-tool por stage e agentificar mídia continuam
  fora de escopo (Fase 2); streaming e judge proxy, fora (Fase 3).

### D33 — Stage `video` agentic: revision apendada, contabilidade de takes e budget por stage
- **Contexto:** o D32 entregou o loop de tool-calling real, mas restrito a `concepts`/
  `scripts` — `_AGENT_STAGES` bloqueava mídia e o D29 exigia ADR próprio para liberá-la.
  Mídia é onde o agente pode agregar mais (reagir a uma take ruim ou a uma falha do
  provider) e também onde errar custa dinheiro de verdade: cada take é uma cobrança.
- **Decisão:** `video` entra em `_AGENT_STAGES`. O agent controla **um** parâmetro,
  `revision` — uma diretiva de uma linha **apendada** ao brief construído pelo server
  (`_video_prompt`), que sempre vence. Reusa o nome `revision` dos stages de texto: os
  brains já ensinam esse vocabulário no system prompt, então nenhum prompt muda.
- **Fronteiras:** `tier` **nunca** entra no schema — vem do tier routing (conditional edge)
  e define o custo (`seedance` ≈ 17x `ltx`); o `product_demo` fixa `ltx` de propósito.
  Idem `item_id`, `seconds`, `attempt` (vem do loop de QC), `system_prompt` e
  `reference_image_uri`. O filtro `safe_inputs` do stage executor (D32) já garante isso:
  o que não está em `properties` o modelo não alcança.
- **Erros viram feedback:** uma exceção da tool dentro do loop vira tool_result de erro e
  volta ao modelo, que pode ajustar a `revision` e tentar de novo dentro do budget. Se o
  budget acabar **sem nenhum sucesso**, o último erro **propaga** — o stage nunca retorna
  sucesso falso, e a safety-net não roda (seria outra chamada paga fadada ao mesmo erro).
- **Contabilidade de takes:** `run_agent_loop` passa a devolver `AgentRunResult` (output
  final + todas as `ToolAttempt`). O node de vídeo cobra **todas** as takes bem-sucedidas,
  não só a vencedora — antes o custo do run mentiria. As descartadas **não** vão para
  `item.clips`: o `IntegrityQCAdapter` valida cada clip do item, então uma take rejeitada
  reprovaria o item inteiro e furaria `qc.required_clip_count`. Elas ficam como
  proveniência no meta do clip final (`agent_takes`, `superseded_takes` com
  uri/cost_usd/revision). Os bytes não são persistidos: ninguém os consome, e o custo já
  está contabilizado. **Limitação conhecida:** uma take que falhe *depois* de o provider
  cobrar não é contabilizada (exigiria custo no path de exceção do adapter).
- **Budget por stage:** `agent.max_steps_by_stage.video: 2` (uma take base + uma revisão),
  porque uma rodada de texto custa centavos e uma de vídeo, dólares. E `max_tool_calls`
  (novo): `max_steps` conta **rodadas do modelo**, não chamadas de tool — um único step
  pode pedir N takes, então só o cap de chamadas segura o custo de fato.
- **Assimetria deliberada:** nos stages de texto o `revision` é repassado ao adapter, que o
  apenda ao prompt lá dentro; em vídeo o append acontece **na tool**, para o `VideoPort` e
  os adapters (`mock`, `replicate_video`) não mudarem.
- **Consequência:** o agent passa a dirigir geração de mídia, coberto offline (adapter
  agentic fake + brain determinístico) e sem custo. `config-mock` mantém `video` em modo
  tool (dry-run barato); o caminho agentic é provado por testes de node e por um e2e do
  grafo inteiro. `roster`/`assembly`/`upscale` seguem fora até terem contrato de artefato
  próprio; multi-tool por stage segue YAGNI (nenhum stage tem 2 tools legítimas).

### D34 — Streaming de tokens no GatewayLLMAdapter (SSE)
- **Contexto:** o D31 deixou streaming de fora e só o `AnthropicLLMAdapter` emitia tokens
  (via `messages.stream` do SDK). Mas o adapter LLM **default do perfil live** é o
  `GatewayLLMAdapter` — ou seja, na prática o dashboard nunca via token nenhum.
- **Decisão:** `_chat` ganha um parâmetro `stage`. Quando `stage` é informado **e** há um
  subscriber no `stream_bus`, o POST vai com `"stream": true` e a resposta é consumida como
  SSE, emitindo `llm_start`/`llm_token`/`llm_end` — os mesmos eventos que o front já
  consome (`useRunStream.ts`), sem mudança de contrato.
- **`stage` como gate:** só chamadas que **nomeiam um stage** podem streamar. As rodadas de
  decisão do agent (`_GatewayAgentBrain.complete`) não passam `stage` e portanto nunca
  streamam — paridade com o Anthropic, e evita ter de remontar `tool_calls` fragmentados
  do SSE (deltas indexados com `arguments` em pedaços). O usuário vê o conceito sendo
  escrito, não a deliberação do agent.
- **Shape único:** `_consume_sse` remonta `{"choices":[{"message":{"content": ...}}],
  "usage": ...}` — o equivalente ao `get_final_message()` do SDK Anthropic. Streaming muda
  **como** o texto chega, não **o que** o modelo produz, então `_message_text` e
  `record_llm_usage` seguem inalterados e quem chama não sabe qual ramo rodou.
- **Custo:** o SSE OpenAI-compatible omite `usage` por padrão — sem
  `stream_options: {"include_usage": true}` o custo do run seria zero. Pedimos o usage e o
  lemos do chunk final. Se ainda assim vier ausente, `_openai_usage_to_metric(None)` devolve
  zeros — exatamente o que o caminho não-streaming já faz; sem novo modo de falha.
- **Retry:** seguro streamando **por construção**: `_is_retryable` (`_retry.py`) só cobre
  erros pré-envio (`ConnectError`/`ConnectTimeout`/`PoolTimeout`) e `429`, todos anteriores
  ao 1º token; falha no meio do stream é `ReadTimeout`, explicitamente não retentável. Logo
  um retry nunca reemite tokens. O `llm_start` só sai **depois** do status OK, para um
  retry pré-envio não piscar a UI.
- **Bug de UI corrigido junto (front):** em modo agent o mesmo stage gera mais de uma vez
  (draft -> revisão), e o reducer acumulava `text` entre gerações — a 2ª grudava na 1ª e o
  painel mostrava dois JSONs concatenados. `llm_start` passa a zerar o buffer do stage
  ("nova geração começando"). Era pré-existente (valia para o Anthropic), mas só ficou
  visível ao ligar streaming no adapter default.
- **Consequência:** o dashboard mostra tokens ao vivo no perfil live. Coberto offline com
  `httpx.MockTransport` servindo SSE. Judge proxy e R2 (D30) seguem fora.

## 2026-07-16

### D35 — Persona batch-level e system prompts por stage agentic
- **Contexto:** `concepts`, `scripts` e `creator` não compartilhavam um retrato de **quem
  fala** — cada stage inferia o tom do próprio `offer`, e nada garantia que o creator do
  roster fosse a mesma pessoa que o script imaginava. Em paralelo, os stages agentic
  (D31–D33) rodavam **sem system prompt próprio**: o único guardrail era a tool tipada, que
  restringe o *formato* da saída, não o comportamento do agent.
- **Persona é um stage do top graph, não um campo de config:** `graph/builder.py` passa a
  rodar `persona -> concepts -> scripts -> concept_review -> roster`, com `node_persona`
  como step 0. Ser um node (e não uma string no `pipeline.yaml`) é o que dá as três
  propriedades que interessam: `BatchState.persona` é **checkpointado** (logo o run é
  resumível com a mesma persona), a persona aparece na timeline como qualquer outro stage,
  e ela pode ser **gerada por um agent** com a mesma máquina dos demais — `write_persona`
  é uma typed tool como as outras, e `persona` entrou em `_AGENT_STAGES`.
- **Persona é reusada, não recopiada:** vai como parâmetro para `generate_concepts` e
  `write_script`, e prefixa o `creator_prompt` no `node_roster` via `_prompt_with_persona`.
  O prompt seguro de imagem **não** é tocado — a persona descreve quem fala, não o que a
  imagem mostra, e misturar os dois reabriria o risco de conteúdo que o provider recusa.
  Nos tools a persona só é repassada `if persona is not None`, então adapter que não a
  aceite segue funcionando.
- **Determinismo preservado, ao custo de um ajuste no mock:** a persona entra no hash do
  mock, o que mudou a distribuição do viés de feedback e quebrou
  `test_feedback_loop_biases_next_cycle`. A correção foi no **mock** (slots enviesados
  privilegiam `bias[0]`), não no teste: o comportamento desejado — o vencedor do ciclo
  anterior domina o próximo — era o que o teste afirmava, e o mock é que o violava.
- **System prompt por stage = `_shared.md` + prompt do stage:** cada stage agentic declara
  `system_prompt_path` no `agents.yaml`; `_load_system_prompt` (`agent_catalog.py`)
  concatena as regras comuns de `prompts/agents/_shared.md` com o prompt do stage. Prompt
  como **arquivo versionado**, não string em Python: revisável em diff, editável sem
  redeploy de código. O `_shared.md` é opcional por construção (se não existir, vale só o
  prompt do stage), então um `config-dir` mínimo continua válido.
- **Path é validado no load, não no uso:** `system_prompt_path` absoluto ou com `..` é
  `ValueError`, assim como arquivo ausente ou vazio. O caminho vem de YAML que o operador
  edita, então o loader trata como entrada não confiável e falha cedo — um prompt vazio que
  passasse silenciosamente viraria um agent sem guardrail nenhum.
- **O texto do prompt não vaza para a API:** `AgentCatalog.as_dict()` expõe
  `system_prompt_path` e `has_system_prompt` (booleano), nunca o `system_prompt` resolvido.
  O texto só trafega internamente, do catálogo para `run_stage_agent`.
- **Perfis divergem por executor, não por prompt:** `config/agents.yaml` usa
  `executor: agent`; `config-mock/agents.yaml` usa `executor: tool`, com os mesmos prompts
  no disco. Dry-run segue offline, determinístico e de custo zero.
- **Consequência:** todo stage downstream tem acesso à persona, e cada stage agentic tem
  guardrail próprio versionado. Segue fora: `roster`/`assembly`/`upscale` agentic, judge
  proxy ao vivo, R2 (D30).
