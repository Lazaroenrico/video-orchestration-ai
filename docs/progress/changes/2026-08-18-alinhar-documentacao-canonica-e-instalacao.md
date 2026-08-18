# Alinhar documentação canônica e instalação reproduzível

Data: `2026-08-18`

## Resultado

Alinhamento da documentação arquitetural e dos diagramas com as decisões canônicas de linguagem (D46/D47), separação explícita de `ToolStrategy` vs *action tools*, demarcação do `CompositeAdapter` como domain/media-only, e proteção determinística de build/CI via lockfile `uv.lock` em modo `--frozen`.

## Mudanças de contrato

- **Dockerfile & CI**: `Dockerfile` consome `uv.lock` em modo `--frozen` (`uv export --frozen` + `uv pip install --no-deps`); workflows do GitHub Actions executam `uv lock --check` e `uv sync --frozen --all-extras`.
- **Decisões arquiteturais**: D17, D18, D19, D29, D31, D32, D33 e D34 marcadas explicitamente em `docs/DECISIONS.md` como *superseded* no escopo de LLM/agentes pelas decisões D46 (`LanguageRuntime`) e D47 (`orchestrator.evaluation.judge`).
- **CompositeAdapter**: documentado formalmente como restrito aos papéis de domínio e mídia (`creator`, `video`, `qc`, `assembly`, `upscale`) em `AGENTS.md`, `CLAUDE.md`, `README.md` e `RESOURCES.md`.
- **ToolStrategy**: documentado em `ADR-D46` e `README.md` como estratégia declarativa de structured output Pydantic para os três estágios criativos (`concepts`, `scripts`, `creator_profiles`), distinguido das *action tools* de domínio com efeitos colaterais (`src/orchestrator/tools/`).
- **Diagramas de arquitetura**: fontes (`docs/generate-system-design-excalidraw.mjs`, `docs/system-design.architecture.json`, `docs/project-architecture.architecture.json`, `docs/project-flow.workflow.json`) e diagramas (`docs/system-design.excalidraw`) atualizados para refletir topologia V2 de 5 fases e providers atuais (Vercel Gateway LLM, OpenAI Image + ElevenLabs Voice Design, Replicate P-Video/LTX + LatentSync, Integrity QC, FFmpeg Assembly).

## RED → GREEN

- **RED:** `Dockerfile` e workflows de CI executavam instalações sem validar o lockfile `uv.lock` em modo `--frozen`, permitindo drift de pacotes; documentação descrevia `CompositeAdapter` roteando chamadas de LLM e diagramas continham referências obsoletas a `Seedance`, `Bark` e `Topaz`.
- **GREEN:** `Dockerfile` atualizado para exportar requirements com hashes a partir de `uv.lock` em modo frozen; workflows do CI configurados com `uv lock --check` e `uv sync --frozen`; documentação e diagramas sincronizados com a arquitetura canônica.
- **REFACTOR:** purga de menções e comentários legados em `README.md`, `.env.example`, `RESOURCES.md`, `AGENTS.md` e `CLAUDE.md`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Container Docker instalava pacotes com resolução dinâmica | `Dockerfile` não copiava `uv.lock` nem usava `--frozen` | Copiar `uv.lock` e usar `uv export --frozen` com instalação de wheels fixadas e hashes |
| CI permitia atualização silenciosa de dependências | `uv sync --all-extras` não incluía `--frozen` nem `--check` | Incluir `uv lock --check` e `uv sync --frozen --all-extras` nos workflows |

## Verificação final

- `uv lock --check` executado com sucesso (zero inconsistências entre `pyproject.toml` e `uv.lock`).
- `uv sync --frozen --all-extras` executado com sucesso (94 pacotes verificados em 3ms).
- `ruff check src/ tests/` com 100% de aprovação (All checks passed).
- `python tests/runtime_ffmpeg_smoke.py` com 100% de aprovação.
- `node docs/generate-system-design-excalidraw.mjs` re-gerou `docs/system-design.excalidraw` (85 elementos).
- Suíte pytest offline executada com 1.253 testes passando.

## Pendências ou bloqueios externos

Nenhum.
