# Injeção de repositório de artifacts no runner e paridade live/replay no benchmark

Data: `2026-08-28`

## Resultado

Correção de dois achados de revisão:
1. Em `src/orchestrator/runner.py`, `run_pipeline` mantém o contexto `open_artifact_repository` aberto durante toda a execução do grafo, captura a instância como `artifact_repository` e injeta em `_build_config` (e nas dependências do grafo). Com `DATABASE_URL`, nenhum arquivo SQLite local de `ArtifactDB` é criado.
2. Em `src/orchestrator/evaluation/model_benchmark.py`, execuções live e replay mantêm paridade estrita na representação e no `structural_reason` para respostas estruturadas que falham em regras de duração ou contagem de palavras. O `structured_response` bruto e o motivo da falha são preservados no cassette de gerações, impedindo a degradação de drafts inválidos em `no structured_response`.

## Mudanças de contrato

- `src/orchestrator/runner.py`: `run_pipeline` instancia e passa o repositório de artifacts ativo através de `_build_config(..., artifact_repository=artifact_repository)`, evitando a instanciação desacoplada de fallback SQLite local em tempo de execução.
- `src/orchestrator/evaluation/model_benchmark.py`:
  - Dataclass `GenerationResult` ganha o campo `raw_response: Optional[dict[str, Any]] = None` para reter o payload estruturado bruto recebido do modelo/agente.
  - Gravação de `generations_record` no cassette JSON preserva `structured_response` bruto mesmo para drafts que não passam em `structural_check`.
  - Replay em `live=False` reavalia o `structured_response` gravado contra `structural_check`, garantindo o mesmo diagnóstico em live e replay.

## RED → GREEN

- **RED:**
  - `tests/test_runtime_contract_resume.py::test_run_pipeline_injects_open_artifact_repository_into_graph_config`: falhou ao constatar que `run_pipeline` injetava instância SQLite desconectada do contexto gerenciado.
  - `tests/test_script_model_benchmark.py::test_live_and_replay_preserve_invalid_structured_draft_and_reason`: falhou com `assert None is not None` ao verificar que o cassette de gerações gravava `structured_response: None` para drafts que falhavam na validação de duração (25s > 16s).
- **GREEN:**
  - `runner.py`: envolvimento da chamada a `_build_config` e execução do grafo dentro de `async with open_artifact_repository(...) as artifact_repository:`.
  - `model_benchmark.py`: preservação de `raw_response` em `GenerationResult` e gravação/reavaliação íntegra do `structured_response` no cassette de gerações.
- Ambas as suítes passaram com sucesso.

### Achado posterior: token OIDC no benchmark live

- **Sintoma:** com apenas `VERCEL_OIDC_TOKEN` configurado, `run_benchmark(...,
  live=True)` chegava ao `BenchmarkJudge` com `Authorization: Bearer ` vazio.
- **Causa:** `run_benchmark` carregava `judge.yaml` antes de resolver o token. A
  expansão dos placeholders ocorria naquele momento e não era refeita quando o
  `_run_async` atribuía o token ao ambiente.
- **Correção:** o token é validado antes de `load_judge` e aplicado diretamente
  ao header em memória da configuração do judge; o runtime continua lendo a
  credencial original (`AI_GATEWAY_API_KEY` ou `VERCEL_OIDC_TOKEN`), sem copiar o
  segredo para outra variável de ambiente. A regressão pública está coberta por
  `test_live_run_uses_vercel_oidc_token_for_judge_authorization`.
- **RED:** o teste observou `Authorization: Bearer ` em vez de
  `Bearer oidc-only-token`.
- **GREEN:** após a correção, o mesmo teste observou exatamente
  `Bearer oidc-only-token`.

Também foi removida a supressão global do `DeprecationWarning` do LangSmith em
`pyproject.toml`, para que avisos upstream permaneçam visíveis durante os testes.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `AssertionError: assert ArtifactDB is not sentinel_repo` em teste de injeção de runner | `_build_config` era chamado antes de `open_artifact_repository` com `artifact_repository=None` | Mover `_build_config` para dentro do contexto `open_artifact_repository` passando `artifact_repository` explicitamente |
| `AssertionError: assert None is not None` no cassette de gerações gravado em live | `_run_async` live gravava `{"draft": generation.draft} if generation.draft else None`, descartando o retorno bruto quando inválido | Salvar `generation.raw_response` no cassette e reavaliar estruturalmente no replay |
| `KeyError: 'video'` em `test_project_config_dirs_ship_valid_agents_yaml` após sincronizar `main` | A resolução combinou a asserção nova de catálogo restrito aos três stages criativos com o loop legado que ainda exigia `video` no catálogo | Remover somente a expectativa incompatível de `video`; a igualdade exata das três chaves continua provando o contrato reduzido |
| `KeyError: 'roster'` no teste de expansão de env dos overlays | O teste de infraestrutura ainda usava um stage legado que D52 retirou do catálogo criativo | Exercitar a mesma expansão com o stage suportado `concepts`, preservando a finalidade do teste |
| `LanguageModelFactory` escolheu o modelo do `.env` em vez do modelo explícito do teste | Uma invocação anterior da CLI carregou `AI_GATEWAY_LLM_MODEL` no processo e a fixture hermética não limpava essa variável | Limpar `AI_GATEWAY_LLM_MODEL` na fixture autouse; testes que precisam do override continuam optando via `monkeypatch` |
| `TypeError: got multiple values` em `create_model(model=...)` e `resolve_model_name(override=...)` | O descriptor dual class/instance repassava o alias nomeado junto com o argumento posicional interno | Normalizar `model`/`override` antes do dispatch e rejeitar explicitamente apenas duplicidade posição + keyword; duas regressões públicas cobrem os caminhos |

## Verificação final

- `uv run ruff check src tests` → `All checks passed!`
- `git diff --check` → limpo sem avisos de whitespace/EOF.
- `uv run pytest --no-cov tests/test_script_model_benchmark.py tests/test_runtime_contract_resume.py` → 37 passed, 2 skipped.
- Após sincronizar `origin/main`, suíte offline sem módulos dependentes de PostgreSQL → 1.443 passed, 4 skipped.
- Suíte completa → 1.478 passed, 4 skipped e 116 erros de setup, todos por `Connection refused` no PostgreSQL local documentado.
- `npm run typecheck` em `front/` → aprovado.
- `uv sync --frozen --all-extras` → lockfile e ambiente consistentes.
- `uv run pytest --no-cov tests/test_progress_docs.py` → 5 passed.

## Pendências ou bloqueios externos

- Testes PostgreSQL de integração de infraestrutura (`tests/test_postgres_*.py`) dependem de um servidor PostgreSQL ativo em `127.0.0.1:5432` (comportamento documentado em `AGENTS.md`).
