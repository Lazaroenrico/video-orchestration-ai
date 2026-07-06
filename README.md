# UGC Orchestrator

Motor de orquestração para a pipeline de **AI UGC em escala** (500+ vídeos/semana)
descrita em `Context.md`. O motor é construído via **TDD** sobre **LangGraph /
LangChain / LangSmith** e permite misturar adapters reais e mock por papel.

- `config-mock/` roda dry-run determinístico, sem chamadas externas e custo zero.
- `config/` é o perfil live atual: LLM + creator + vídeo LTX 2.3 Fast + QC de
  integridade + assembly final Seedance 2.0 via APIs reais, sem mock nos papéis runtime.

## Pipeline (9 passos)

1. Conceitos (Claude) · 2. Scripts (Claude) · 3. Creator reutilizável (GPT Image 2 + Topaz + ElevenLabs) ·
4. Talking-head (LTX/Kling/Seedance) · 5. Product demo · 6. Execução paralela ·
7. QC · 8. Montagem · 9. Loop de feedback.

O motor termina em **montagem**: item aprovado é item que passou no QC e gerou
`assembled`. Distribuição/postagem saiu do escopo do produto.

Cada passo é um node num `StateGraph` do LangGraph. Os adapters de provedores são
abstraídos por protocols e ligados por `config/providers.yaml`, sem mexer no grafo.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
npm install  # necessário só para o bridge live de vídeo final via Seedance
```

## Uso

```bash
orchestrator run --batch 12 --offer "serum X" --config-dir config-mock  # dry-run sem rede
orchestrator status <run_id> --config-dir config-mock                   # relatório do run
orchestrator resume <run_id> --config-dir config-mock                   # retoma no mesmo thread_id
orchestrator list                                                   # lista runs
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config-mock  # loop de feedback mock
```

## Rodar o perfil live

```bash
cp .env.example .env
# preencha as chaves reais no .env:
# AI_GATEWAY_API_KEY, REPLICATE_API_TOKEN, REPLICATE_ELEVENLABS_MODEL
# e os campos/voice ids do modelo ElevenLabs hospedado no Replicate
```

O perfil `config/` já ativa `llm: vercel_gateway_llm`, `creator:
creator_real_replicate`, `video: replicate`, `qc: integrity_qc` e `assembly:
vercel_seedance_assembly`. Os clips intermediários usam LTX 2.3 Fast sem áudio
(`generate_audio: false`); o vídeo final é gerado pelo Seedance 2.0 via Vercel AI
Gateway (`bytedance/seedance-2.0`). O fallback mock do vídeo fica desabilitado no
perfil live.

```bash
orchestrator run --batch 3 --offer "serum X" --config-dir config
```

Passo a passo completo, com a saída esperada de cada comando e como lê-la:
**[`docs/DEMO.md`](docs/DEMO.md)**.

## Dashboard web ("Kinetic Command")

A UI é uma **SPA React (Vite + TypeScript + Tailwind)** em `front/`, buildada para
`front/dist/` e servida pelo FastAPI. São 12 telas navegáveis (Dashboard, Campaigns,
Campaign Detail com gate de aprovação de creators + reroll de voz, Create Campaign,
Concepts & Scripts, Creators Library, Job Queue, Video Review & QC, Integrations,
Analytics, Settings, Publishing Calendar), ligadas a dados reais via `/api/*` + SSE
onde há backend.

A tela Concepts & Scripts hidrata runs checkpointados via `/api/state/{run_id}`, então
ela não depende só do stream SSE em memória. Na galeria de creators, `Draft Video with
<creator>` inicia um novo run com o creator selecionado como roster fixo e abre
`/scripts?run=<novo_run_id>` para revisão/edição antes de gerar vídeo.

```bash
cd front && npm install && npm run build   # gera front/dist (servido em GET /)
orchestrator serve                         # dashboard em http://localhost:8000/
cd front && npm run dev                    # dev: Vite faz proxy /api,/media,/videos -> :8000
```

`front/dist` e `front/node_modules` são gitignored — builde a SPA antes de `orchestrator
serve` (sem o build, `GET /` devolve uma página de fallback instruindo a rodar `npm run
build`, o que mantém o CI sem Node verde). Endpoints principais: `POST /api/run`,
`GET /api/stream/{run_id}` (SSE), `GET /api/state/{run_id}`, `POST /api/approve/{run_id}`,
`GET /api/creators`, `GET /api/prompts`, `GET /api/integrations`, `GET /api/runs`,
`GET /api/status/{run_id}`.

## Testes (TDD)

```bash
pytest                                    # toda a suíte (determinística, sem rede)
pytest tests/test_judge_eval.py           # LLM Judge via cassette (CI)
pytest tests/test_judge_eval.py --live    # LLM Judge contra o gateway real (regrava cassette)
```

**Regra de integridade dos testes:** se um teste falha, investiga-se a causa raiz e corrige-se o
código — nunca se afrouxa a asserção só para passar. Ver `CLAUDE.md`.

## Documentação

- `CLAUDE.md` — guia para sessões do Claude Code (stack, convenções, regra dos testes).
- `docs/DECISIONS.md` — log de todas as decisões + rationale.
- `docs/PROGRESS.md` — handoff: o que está feito, o que falta, próximo passo.
# video-orchestration-ai
