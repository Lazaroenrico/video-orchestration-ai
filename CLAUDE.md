# CLAUDE.md — guia para sessões do Claude Code neste repo

## O que é

Motor de orquestração para a pipeline de **AI UGC em escala** descrita em `Context.md`
(9 passos: conceitos → scripts → creator → talking-head → product demo → execução
paralela → QC → montagem → feedback). **v1 = só o motor**, em modo
**mock/dry-run** (sem chamadas externas reais, custo zero). Integrações reais (Claude,
GPT Image 2, Topaz, ElevenLabs, Replicate/fal/AtlasCloud) entram depois, adapter a adapter.
Distribuição/postagem está fora do escopo: o estado terminal aprovado é `assembled`.

## Stack e papéis

- **LangGraph** — motor de orquestração. `StateGraph` (cada stage é um node), fan-out
  paralelo via `Send`, conditional edges para **tier routing** e **QC gate/loop**,
  **checkpointer** (AsyncSqliteSaver) para resumibilidade (`thread_id` = run id).
- **LangChain** — adapters/LLM abstraídos. No v1 só o `MockAdapter`.
- **LangSmith** — tracing e avaliação do LLM Judge. Tracing é automático quando
  `LANGSMITH_TRACING=true` e `LANGSMITH_API_KEY` estão setados (nada a codar — vem de
  usar LangChain/LangGraph). Sem as envs, roda offline.

## Layout

```
config/         pipeline.yaml (knobs), providers.yaml (provider->adapter), judge.yaml (gateway)
src/orchestrator/
  graph/        state.py, routing.py, builder.py, checkpoint.py
  nodes/        base.py, stages.py  (os stages da pipeline como nodes; mocks no v1)
  adapters/     base.py (Protocols), mock.py, judge.py (gateway + cassette)
  registry.py   resolve provider->adapter
  config.py     carga dos YAMLs
  runner.py     run/resume/status/list + relatório
  cli.py        entrypoint click
tests/          test_*.py + cassettes/ (goldens do judge)
docs/           DECISIONS.md, PROGRESS.md
```

## Comandos

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
orchestrator run --batch 12 --offer "serum X" --config-dir config   # pipeline mock
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config  # N ciclos encadeados
orchestrator status <run_id> --config-dir config
orchestrator resume <run_id> --config-dir config
orchestrator list

pytest                                 # suíte (determinística, offline)
pytest tests/test_judge_eval.py        # LLM Judge via cassette
pytest tests/test_judge_eval.py --live # LLM Judge contra o gateway real (regrava cassette)
```

Nota sobre testes: o hook do `rtk` colapsa a saída do pytest para "No tests collected".
Para ver o resultado real, rode `rtk proxy python -m pytest ...`.

## Convenções

- **TDD estrito**: teste primeiro (red), implementação mínima (green), refactor. A
  ordem está em `docs/PROGRESS.md`.
- **Async**: nodes e adapters são `async`; o grafo roda via `ainvoke`. Por isso o
  checkpointer é `AsyncSqliteSaver` (não o `SqliteSaver` sync).
- **Determinismo**: nada de `random`. Mocks derivam tudo de hash dos inputs; ids de
  item vêm do id do conceito. Isso é o que torna os testes e o `--dry-run` reproduzíveis.
- **Nodes que precisam de `config`**: o parâmetro precisa ser tipado como
  `RunnableConfig`, senão o LangGraph não injeta o config.

## Regra de integridade dos testes (inegociável)

Todos os testes devem passar. Quando um falha, **investigue a causa raiz e corrija o
código** (ou o teste, só se ele estava errado quanto ao comportamento desejado).
**Nunca** afrouxe uma asserção, remova um caso, marque `xfail`/`skip` ou troque o valor
esperado só para ficar verde. Um teste verde tem que significar comportamento correto.
Registre toda falha investigada (sintoma → causa → correção) em `docs/PROGRESS.md`.
(Skips legítimos: testes `--live` que exigem infra externa — opt-in, documentados.)

## Como plugar um adapter real (depois)

1. Implemente os Protocols de `adapters/base.py` num novo adapter.
2. Registre em `registry.py` (`register_adapter("replicate", factory)`).
3. Troque o nome em `config/providers.yaml` (ex.: `video: replicate`).
4. O grafo não muda. Rode com `--no-dry-run` quando os adapters reais estiverem ligados.

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). Format flags (-c, -l, -L, -o, -Z) run raw.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->

<!-- ai-memory:start -->
## Long-term memory (ai-memory)

This project uses [ai-memory](https://github.com/akitaonrails/ai-memory)
for cross-session continuity.

**Default to the current project - always.** Every ai-memory tool
auto-scopes to the project resolved from your session's working
directory. **Do NOT pass `project`, `workspace`, or `cwd` arguments unless
the user explicitly references a *different* project by name** (e.g. "what
did we decide in the `other-app` project?"). Phrases like "this project",
"here", "we", "our work", and "where did we leave off" all mean the
*current* project, so call tools with no scoping args.

This default assumes the MCP client can identify the current agent
session. Static MCP clients in parallel sessions for the same user cannot
forward the real agent session id automatically; pass explicit
`workspace` + `project` / `scopes`, or use a session-aware bridge that
forwards the lifecycle-hook session id on MCP calls.

**Lifecycle hooks already capture every prompt and tool call
automatically.** Do not manually write routine notes. Only write durable
memory when the user explicitly asks to remember or annotate something
permanently.

### Use the installed ai-memory Agent Skills

Detailed tool-routing guidance lives in the installed ai-memory Agent
Skills. When a task matches an installed ai-memory Agent Skill, load and
follow that skill before calling ai-memory tools. The skills cover memory
retrieval, handoffs, durable pages, learning maintenance, and routing
install or refresh work.

### When you write a project rule, write it here

If you're about to write a durable project rule ("always X", "never
Y", "all PRs must ..."), write it in the project's canonical agent instruction file.
Many projects use CLAUDE.md for Claude Code and
AGENTS.md for Codex / OpenCode / Cursor / Gemini CLI, but if the project
says one file is canonical, use that file.

### Refreshing this snippet

This block is maintained by ai-memory. Two ways to refresh it with the
latest binary's recommended copy:

- **From the agent** (no terminal needed): ask "refresh the ai-memory
  routing in this project". The agent calls `memory_install_self_routing`,
  picks the right filename for itself (Claude Code -> `CLAUDE.md`; Codex /
  OpenCode / Cursor / Gemini -> `AGENTS.md`), uses its Write / Edit tool
  to replace or append the returned `markered_block` while preserving
  non-ai-memory user content, then writes or updates each returned
  `managed_skills` item under the selected skill root from `target_hints`
  using its `relative_path`.
- **From the CLI**: `ai-memory install-instructions` (defaults to
  `CLAUDE.md`; pass `--target AGENTS.md` for non-Claude agents or projects
  that use `AGENTS.md` as the canonical instruction file).

Both are idempotent: re-runs replace the block bracketed by
`<!-- ai-memory:start -->` / `<!-- ai-memory:end -->` markers without
disturbing the rest of the file.
<!-- ai-memory:end -->
` markers without
disturbing the rest of the file.
<!-- ai-memory:end -->
