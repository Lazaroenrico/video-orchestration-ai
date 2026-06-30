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

### D11 — Tier routing escalonado e custo acumulado
- **Decisão:** tentativas de QC escalam o tier (LTX→Kling→Seedance), espelhando o
  Context (bulk barato, vencedores no premium); cada geração acumula custo.
- **Consequência:** o loop de regeneração termina (qualidade sobe com o tier) e o
  relatório mostra custo por tier — fiel ao "The Cost at Scale".

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
  6. `.env.example` — atualizar status de `TOPAZ_API_KEY` e `ELEVENLABS_API_KEY` para live.
- **Env vars necessárias para MVP:**

  | Variável | Para quê | Obrigatória |
  |---|---|---|
  | `AI_GATEWAY_API_KEY` | Token único Vercel Gateway (LLM + imagem) | Sim |
  | `TOPAZ_API_KEY` | Upscale direto Topaz Labs | Sim |
  | `ELEVENLABS_API_KEY` | Voz direto ElevenLabs | Sim |
  | `REPLICATE_API_TOKEN` | Vídeo via Replicate | Sim |
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
