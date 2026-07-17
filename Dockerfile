# syntax=docker/dockerfile:1
# ============================================================================
# Imagem OCI única e portável— Linux/amd64.
#
# Contém Python 3.12 + Node LTS porque o bridge de montagem Seedance
# (scripts/vercel_generate_video.mjs) é chamado via `node` por
# adapters/vercel_seedance_assembly.py. Um ENTRYPOINT, três papéis:
#     orchestrator api      → FastAPI/dashboard/SSE
#     orchestrator runner   → executa a pipeline
#     orchestrator migrate  → materializa o estado local (schema/dirs)
#
# A mesma imagem roda local (docker/compose), na Cloudflare Containers e no
# AWS ECS Fargate — sem rebuild. Disco é efêmero: só temporários de uma chamada.
# ============================================================================

# --- Stage 1: build da SPA (Kinetic Command) --------------------------------
FROM node:22-bookworm-slim AS front-build
WORKDIR /front
COPY front/package.json front/package-lock.json ./
RUN npm ci
COPY front/ ./
RUN npm run build   # gera /front/dist

# --- Stage 2: runtime Python 3.12 + Node LTS --------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Node LTS copiado da imagem oficial (mesma base bookworm → glibc compatível),
# evitando um apt/nodesource extra. O bridge Seedance precisa dele em runtime.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ORCH_SERVE_LOCAL_MEDIA=0

WORKDIR /app

# Dependências Python (extra [web] traz fastapi/uvicorn p/ o papel `api`).
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache -e ".[web]"

# Dependência Node do bridge Seedance (o `ai` SDK). node_modules precisa ficar
# na raiz do repo porque o script resolve `import 'ai'` a partir do cwd (=/app).
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# Código de app: configs, scripts e a SPA já buildada.
COPY config/ ./config/
COPY config-mock/ ./config-mock/
COPY scripts/ ./scripts/
COPY --from=front-build /front/dist ./front/dist

# Usuário não-root; disco efêmero. Cria .orchestrator já com dono `app` para que o
# volume nomeado do compose herde essa permissão (senão o Docker o cria como root e o
# usuário não-root não escreve o SQLite).
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.orchestrator \
    && chown -R app:app /app
USER app

EXPOSE 8000
ENTRYPOINT ["orchestrator"]
CMD ["api", "--host", "0.0.0.0", "--port", "8000"]
