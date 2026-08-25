# Runtime mode centralizado e knobs de ambiente

Data: `2026-08-25`

## Resultado

Toda decisão de modo de execução agora passa por `src/orchestrator/runtime_mode.py`:
`database_url()` (leitura lazy de `DATABASE_URL` a cada chamada), `is_durable()`,
`paid_adapters_enabled()` (parse de `ORCH_ENABLE_PAID_ADAPTERS`) e
`open_repository_backend()` (idioma comum das fachadas de store). As cinco fachadas
(`run_store`, `job_store`, `creator_store`, `prompt_store`, `feedback_store`),
`storage/db.py` e o branch do checkpointer (`graph/checkpoint.py`) foram migrados;
os dois parses espalhados de `ORCH_ENABLE_PAID_ADAPTERS` (`tools/base.py`,
`worker.py`) também. O singleton de throttle Replicate ganhou teardown automático
no conftest, então mudanças nas envs `REPLICATE_*` via `monkeypatch` valem entre
testes. Nenhum estado global novo; as checagens continuam lazy (lêem env no ponto
de chamada, nunca no import).

## Mudanças de contrato

Nenhuma assinatura pública mudou. Detalhes observáveis:

- `open_repository_backend(local_factory, postgres_factory)` é o novo idioma interno
  das fachadas; os yields locais/PostgreSQL e os imports tardios da stack PostgreSQL
  permanecem idênticos (a stack só carrega em modo durável).
- `adapters/_throttle.reset_replicate_throttle()` (já existente) passou a ser
  chamado no teardown do fixture autouse `tests/conftest.py::_force_mock_providers`.
- `tools/base.py` e `worker.py` consomem o knob via chamada qualificada
  `runtime_mode.paid_adapters_enabled()`, mantendo o módulo como única costura
  testável (ver Falhas investigadas).

## RED → GREEN

- **RED:** `tests/test_runtime_mode.py` recebeu quatro discriminadores que falharam
  contra o código antigo:
  - `test_facades_route_through_runtime_mode_selector` — com `DATABASE_URL`
    presente e `runtime_mode.is_durable` desligado, as fachadas antigas liam
    `os.environ` direto e tentavam abrir PostgreSQL (AssertionError no stub de
    `get_shared_database`).
  - `test_open_checkpointer_reads_database_url_via_runtime_mode` — com a URL
    presente e `runtime_mode.database_url` desligado, o checkpointer antigo ia para
    o branch PostgreSQL em vez do SQLite local.
  - `test_execute_paid_effect_gates_via_runtime_module` — com a env `true` mas o
    knob central desligado, o gate antigo (parse direto) deixava passar até o erro
    de ledger em vez do erro de knob.
  - `test_next_test_starts_with_cold_throttle` (par com
    `test_throttle_left_warm_is_reset_for_next_test`) — sem o teardown no conftest,
    o singleton aquecido por um teste vazava para o seguinte.
- **GREEN:** migração mecânica dos call-sites listados acima; fachadas delegam a
  `open_repository_backend`; `checkpoint.py` troca `os.environ.get("DATABASE_URL")`
  por `runtime_mode.database_url()`; `tools/base.py`/`worker.py` usam
  `runtime_mode.paid_adapters_enabled()`; conftest reseta o throttle após cada teste.
- **REFACTOR:** imports mortos removidos (`import os` em seis arquivos onde a
  única leitura era justamente a migrada); chamadas qualificadas por módulo nos
  parses para preservar a costura única de patch.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `test_execute_paid_effect_gates_via_runtime_module` continuou vermelho após migrar `tools/base.py`. | `from orchestrator.runtime_mode import paid_adapters_enabled` vincula a função original no namespace de `tools.base` no import; o patch em `runtime_mode.paid_adapters_enabled` não surtia efeito. | Consumo por atributo de módulo (`runtime_mode.paid_adapters_enabled()`), resolvido na chamada; mesmo padrão adotado em `worker.py`. |

## Verificação final

- Bateria alvo (185 passed):
  `pytest tests/test_runtime_mode.py tests/test_creator_store.py tests/test_feedback_store.py tests/test_artifact_db.py tests/test_replicate_throttle.py tests/test_checkpoint.py tests/test_runner_service.py tests/test_sqs_runner.py tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_retention.py tests/test_retention_wiring.py tests/test_media_persistence_wiring.py --no-cov -p no:cacheprovider`
- Adjacentes (52 passed): `tests/test_tools.py tests/test_web_prompts.py tests/test_feedback_loop.py`.
- RED documentado antes do GREEN (4 falhas exibidas acima, executadas contra o código pré-migração).
- `ruff check` limpo em todos os arquivos tocados (E/F/I).
- Testes PostgreSQL (`test_postgres_*`) não executados: exigem servidor em
  `127.0.0.1:5432`; limitação de infraestrutura local já registrada em AGENTS.md.
  Os caminhos duráveis dessas fachadas continuam cobertos pelos discriminadores com
  stubs de `get_shared_database`.

## Pendências ou bloqueios externos

Branches de `DATABASE_URL`/knob **não** migrados, com motivo:

- `web/server.py` (10 leituras de `DATABASE_URL`, 1 de `ORCH_ENABLE_PAID_ADAPTERS`)
  — arquivo interditado nesta tarefa (agente paralelo trabalhando nele).
- `tools/video.py:553` (`ORCH_ENABLE_PAID_ADAPTERS`) — arquivo interditado nesta
  tarefa pelo mesmo motivo.
- `cli.py:392` e validações `MIGRATION_DATABASE_URL` — são mensagens de erro de
  entrada do usuário na CLI de migração (exigem URL explícita), não seleção de modo.
- `db/database.py:62` — guarda interna da própria stack PostgreSQL (raise quando a
  URL falta); é o callee do seletor central, não um branch espalhado.

`docs/PROGRESS.md` não foi atualizado nesta tarefa (interdição explícita do enunciado).
