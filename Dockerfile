# syntax=docker/dockerfile:1
# ============================================================================
# Imagem OCI única e portável — Linux/amd64.
#
# Contém Python 3.12 + FFmpeg. Um ENTRYPOINT, três papéis:
#     orchestrator api      → FastAPI/dashboard/SSE
#     orchestrator runner   → executa a pipeline
#     orchestrator migrate  → materializa o estado local (schema/dirs)
#
# A mesma imagem roda local (docker/compose), na Cloudflare Containers e no
# AWS ECS Fargate — sem rebuild. Disco é efêmero: só temporários de uma chamada.
# ============================================================================

# --- Stage 1: build da SPA (Kinetic Command) --------------------------------
FROM node:22.22.3-bookworm-slim AS front-build
WORKDIR /front
COPY front/package.json front/package-lock.json ./
RUN npm ci
COPY front/ ./
RUN npm run build   # gera /front/dist

# --- Stage 2: runtime Python 3.12 -------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Montagem final determinística e validação de streams.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ORCH_SERVE_LOCAL_MEDIA=0

WORKDIR /app

# Dependências Python (extra [web] traz fastapi/uvicorn p/ o papel `api`).
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv export --frozen --extra web --no-dev --output-file /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps -e ".[web]" \
    && rm /tmp/requirements.txt

# Código de app: configs, scripts e a SPA já buildada.
COPY config/ ./config/
COPY config-mock/ ./config-mock/
COPY config-staging/ ./config-staging/
COPY alembic.ini ./alembic.ini
COPY migrations/ ./migrations/
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
