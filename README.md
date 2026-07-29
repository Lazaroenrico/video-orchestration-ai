# UGC Orchestrator

Motor de orquestração para a pipeline de **AI UGC em escala** (500+ vídeos/semana)
descrita em `Context.md`. O motor é construído via **TDD** sobre **LangGraph /
LangChain / LangSmith** e permite misturar adapters reais e mock por papel.

- `config-mock/` roda dry-run determinístico, sem chamadas externas e custo zero.
- `config/` é o perfil live atual: LLM + creator + clips PrunaAI P-Video,
  locução ElevenLabs e montagem FFmpeg + QC de integridade, sem mock nos papéis runtime.

## Pipeline (9 passos)

1. Conceitos (Claude) · 2. Scripts (Claude) · 3. Creator reutilizável (GPT Image 2 + ElevenLabs) ·
4. Talking-head (PrunaAI P-Video) · 5. Product demo (PrunaAI P-Video) · 6. Execução paralela ·
7. QC · 8. Locução (ElevenLabs Turbo v2.5) · 9. Montagem (FFmpeg) · 10. Loop de feedback.

O motor termina em **montagem**: item aprovado é item que passou no QC e gerou
`assembled`. Distribuição/postagem saiu do escopo do produto.

Cada passo é um node num `StateGraph` do LangGraph. Os adapters de provedores são
abstraídos por protocols e ligados por `config/providers.yaml`, sem mexer no grafo.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
npm install  # necessário apenas se habilitar o adapter de vídeo Vercel opt-in
```

## Uso

```bash
orchestrator run --batch 12 --offer "serum X" --config-dir config-mock  # dry-run sem rede
orchestrator status <run_id> --config-dir config-mock                   # relatório do run
orchestrator resume <run_id> --config-dir config-mock                   # retoma no mesmo thread_id
orchestrator list                                                   # lista runs
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config-mock  # loop de feedback mock
```

## Desenvolvimento local com um comando

```bash
cp .env.example .env
# preencha no .env:
# AI_GATEWAY_API_KEY, REPLICATE_API_TOKEN, REPLICATE_ELEVENLABS_MODEL,
# R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY e R2_BUCKET
./scripts/dev-local up
```

O comando detecta Docker Compose V2 ou V1, valida a configuração sem imprimir
secrets e sobe PostgreSQL 16, migrações, API com runner embutido e Vite com hot
reload. Acesse:

- aplicação: `http://localhost:5173`
- API/readiness: `http://localhost:8000/readyz`
- PostgreSQL: `127.0.0.1:55432`

Dentro dos containers, `DATABASE_URL` e `MIGRATION_DATABASE_URL` sempre apontam
para o PostgreSQL local; qualquer URL de banco existente no `.env` é ignorada. O
R2 é o storage padrão e continua externo ao ciclo de vida do Compose. Para parar
preservando o banco ou zerar todos os volumes locais:

```bash
./scripts/dev-local down
./scripts/dev-local reset --yes  # não remove objetos do R2
```

O default usa `config/`: Vercel AI Gateway para LLM e GPT Image 2; Replicate para
ElevenLabs e os dois clips; FFmpeg local para concatenação e áudio; R2 para os
bytes. Os clips usam `prunaai/p-video` em draft 1080p (`US$ 0,01/s`) durante a
validação E2E. A montagem não faz uma terceira geração paga.
Adapters pagos estão
habilitados, mas só há custo quando uma campanha é iniciada. Para validar toda a
infra sem geração paga, use o perfil staging; para trocar também o storage por
filesystem local, use o override explícito:

```bash
ORCH_DEV_CONFIG_DIR=config-staging ./scripts/dev-local up
ORCH_DEV_CONFIG_DIR=config-staging ORCH_DEV_STORAGE_BACKEND=local \
  ./scripts/dev-local up
```

O roteiro live é limitado a 35 palavras/14 segundos. Após o QC, a voz aprovada
gera `voiceover`, e o FFmpeg produz um MP4 vertical de 16 segundos com H.264/AAC.
O primeiro run live deve usar batch 1. O banco persiste runs, jobs, gates,
eventos, checkpoints e retries; ponteiros de mídia permanecem `r2://` no estado e
as respostas REST/SSE geram URLs assinadas somente na saída.

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
- `docs/PLAN-CREATOR-VOICE-DESIGN.md` — plano para derivar e aprovar a voz de cada
  creator sem pools de IDs no `.env`.
- `docs/PROGRESS.md` — handoff: o que está feito, o que falta, próximo passo.
# video-orchestration-ai
