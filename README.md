# UGC Orchestrator

Motor de orquestração para a pipeline de **AI UGC em escala** (500+ vídeos/semana) descrita em `Context.md`. O motor é construído via **TDD** sobre **LangGraph / LangChain / LangSmith** e permite alternar ou misturar adapters reais e mock por papel.

A API/UI V2 expõe a pipeline em **5 Fases Operacionais**:
1. **Configuração** — Parâmetros iniciais da campanha, oferta, platforming e batch size.
2. **Plano Criativo** — Geração de conceitos e scripts via LLM + definição da persona/aparência do criador.
3. **Revisão (Human Gate V2)** — Gate único de aprovação criativa com edição de conceitos e preview/reroll de vozes dos criadores.
4. **Produção e QC** — Geração paralela de vídeos com tier routing de modelos (custo vs. qualidade) e QC automatizado de áudio/vídeo.
5. **Montagem** — Síntese final da locução (ElevenLabs) e montagem de áudio/vídeo (FFmpeg) gerando o produto final em estado `assembled`.

*(Nota: Distribuição e postagem automática em redes sociais estão fora do escopo do orquestrador; o estado terminal aprovado do vídeo é `assembled`.)*

---

## Perfis de Configuração (`config*`)

O orquestrador suporta três perfis de execução configuráveis:

- **`config-mock/`** — Execução determinística local, sem chamadas de rede externas e custo zero (ideal para desenvolvimento offline e suíte de testes).
- **`config-staging/`** — Provedores AI em modo mock, porém utilizando a infraestrutura real de persistência e filas (PostgreSQL, R2/S3, outbox e Runner worker). Usado por padrão no desenvolvimento local durável.
- **`config/`** — Perfil live completo com adapters reais de LLM (Vercel AI Gateway / OpenAI / Claude), Criadores (GPT Image 2 + Topaz + ElevenLabs), Vídeo (PrunaAI P-Video / Replicate), QC de áudio/vídeo e Montagem local via FFmpeg. Em ambiente durável, adapters pagos exigem a variável `ORCH_ENABLE_PAID_ADAPTERS=true`.

---

## Setup e Requisitos

### Pré-requisitos
- Python 3.12+
- `uv` (gerenciador de pacotes Python)
- Node.js LTS + `npm` (para a SPA em `front/`)
- Docker & Docker Compose (para ambiente local durável com PostgreSQL 16)

### Instalação

```bash
# Clone o repositório e configure a venv
uv venv --python 3.12
uv pip install -e ".[dev]"

# Instale dependências do frontend (opcional para rodar somente a CLI)
cd front && npm install && npm run build && cd ..
```

---

## Desenvolvimento Local com um Comando

O projeto inclui o utilitário `./scripts/dev-local` para subir toda a stack em containers (PostgreSQL 16, migrações Alembic, API FastAPI com Runner embutido e dev server do Vite com hot-reload):

```bash
# 1. Copie o arquivo de variáveis de ambiente
cp .env.example .env

# 2. Suba o ambiente de desenvolvimento local
./scripts/dev-local up
```

Serviços disponibilizados:
- **Aplicação Web / Dashboard**: `http://localhost:5173` (Vite dev server)
- **API FastAPI / Readiness**: `http://localhost:8000/readyz`
- **PostgreSQL**: `127.0.0.1:55432`

Para encerrar o ambiente preservando os dados ou limpar todos os volumes locais:

```bash
./scripts/dev-local down
./scripts/dev-local reset --yes
```

---

## Dashboard Web ("Kinetic Command")

Interface SPA desenvolvida em **React 19 + TypeScript + Vite + Tailwind CSS** localizada em `front/`, consumindo a API REST e stream de eventos SSE em tempo real. Utiliza **TanStack Query** com persistência em `localStorage` para navegação instantânea e cache resiliente.

Possui **12 telas operacionais**:
- **Dashboard**: Métricas gerais, campanhas ativas, atalhos de retry e progresso em tempo real.
- **Campaigns**: Gestão, busca e filtros de todas as campanhas executadas.
- **Campaign Detail**: Visualização do progresso, Human Gate V2 (aprovação criativa e reroll de voz) e Retry manual de campanhas falhadas.
- **Create Campaign**: Wizard de criação de novas campanhas.
- **Concepts & Scripts**: Galeria de conceitos criados e seus respectivos roteiros.
- **Creators Library**: Biblioteca de personas de criadores gerados.
- **Job Queue**: Fila de execução de jobs duráveis do PostgreSQL/Runner.
- **Video Review & QC**: Central de revisão de vídeos gerados e relatório do QC.
- **Integrations**: Status de conexão com os provedores de IA.
- **Analytics**: Desempenho e taxa de aprovação/rejeição.
- **Settings**: Configurações da plataforma e chaves.
- **Publishing Calendar**: Calendário de agendamento visual (out-of-scope para postagem direta).

### Desenvolvimento Frontend

```bash
cd front
npm run typecheck    # Checagem de tipos com tsc
npm run build        # Build de produção para front/dist/
npm run dev          # Server de dev Vite com proxy para o backend em :8000
```

`front/dist` e `front/node_modules` são mantidos no `.gitignore`. O FastAPI serve automaticamente `front/dist/index.html` em `GET /` quando ele é compilado.

---

## Uso da CLI (`orchestrator`)

O executável `orchestrator` fornece comandos para execução offline, gestão de banco de dados e disparo do runner worker.

### Comandos de Pipeline
```bash
# Roda uma pipeline em dry-run (config-mock)
orchestrator run --batch 12 --offer "serum X" --config-dir config-mock

# Roda múltiplos ciclos encadeados com feedback loop (close-the-loop)
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config-mock

# Exibe o status e relatório de um run pelo ID
orchestrator status <run_id> --config-dir config-mock

# Retoma a execução de um run a partir do checkpointer
orchestrator resume <run_id> --config-dir config-mock

# Lista os IDs de runs armazenados
orchestrator list
```

### Comandos de Servidor e Workers (OCI / Durável)
```bash
# Inicia a API REST + SSE FastAPI + SPA
orchestrator api --port 8000

# Worker durável: consome 1 job pendente da fila PostgreSQL
orchestrator runner --once

# Servidor interno de launcher para containers runner
orchestrator runner-service

# Consumidor de filas SQS/Cloudflare que dispara jobs duráveis do PostgreSQL
orchestrator sqs-runner
```

### Administrador de Banco de Dados e Migrações
```bash
# Executa as migrações Alembic no PostgreSQL (ou configura SQLite/ArtifactDB local)
orchestrator migrate

# Importa dados e mídias legadas de SQLite/JSON para o PostgreSQL/R2 de forma idempotente
orchestrator import-legacy --apply

# Gestão de organizações e permissões (multi-tenant)
orchestrator db org-create --slug acme --name "Acme Corp"
orchestrator db membership-grant --user-id <usr_id> --org-id <org_id> --role admin

# Diagnóstico operacional e manutenção
orchestrator ops inspect-run <run_id>
orchestrator ops maintain --purge-expired
orchestrator storage migrate-run <run_id>
```

---

## Arquitetura e Engenharia

- **Engine de Orquestração**: **LangGraph** (`StateGraph` assíncrono com suporte a fan-out paralelo `Send` e conditional routing para Tier Routing de vídeo e QC loop).
- **Checkpointer Resumível**: `AsyncPostgresSaver` com **Row Level Security (RLS)** habilitado por `organization_id` no PostgreSQL, garantindo isolamento multi-tenant completo.
- **Pattern Outbox & Workers**: Concorrência otimista nos jobs (`FOR UPDATE SKIP LOCKED`), leases com heartbeat de 30s e isolamento por `PostgresEffectLedger` para impedir cobranças duplicadas em provedores pagos.
- **Gate Humano V2**: Ponto único de interrupção (`review_creative_plan`). A aprovação/edição é enviada via `POST /api/v2/runs/{run_id}/review` com verificação de versão para evitar conflitos de concorrência (*stale gate rejection*).
- **Forks Limpos para Retry**: Repetir uma campanha em erro gera um novo ID de campanha (`web-...`) mantendo o registro original intacto para fins de auditoria.

---

## Testes (TDD Estrito)

A suíte de testes garante 100% de integridade funcional. É uma regra do projeto **nunca afrouxar asserções** para obter testes verdes.

```bash
# Executar a suíte de testes backend via rtk proxy
rtk proxy python -m pytest --no-cov tests/

# Executar testes do LLM Judge usando cassette (CI/offline)
rtk proxy python -m pytest tests/test_judge_eval.py

# Executar testes do LLM Judge contra o gateway real (regrava cassette)
rtk proxy python -m pytest tests/test_judge_eval.py --live
```

---

## Documentação Adicional

- `AGENTS.md` — Regras do agente, arquitetura de nodes e convenções do projeto.
- `Context.md` — Visão conceitual e operacional da pipeline de AI UGC em escala.
- `docs/DECISIONS.md` — Registro de Decisões de Arquitetura (ADRs).
- `docs/PROGRESS.md` — Log detalhado de progresso e correções Red → Green.
- `docs/DEMO.md` — Passo a passo detalhado de demonstração e saída de comandos.
