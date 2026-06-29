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

### D9 — Checkpointer assíncrono (AsyncSqliteSaver)
- **Contexto:** os nodes são async e o grafo roda via `ainvoke`; o `SqliteSaver` sync
  lança `NotImplementedError` em métodos async.
- **Decisão:** usar `AsyncSqliteSaver` (+ `aiosqlite`), construído manualmente com um
  `JsonPlusSerializer` que registra os tipos pydantic do estado (`Item`/`Artifact`/
  `QCResult`) em `allowed_msgpack_modules`.
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
