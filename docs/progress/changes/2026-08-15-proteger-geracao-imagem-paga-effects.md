# Proteção da geração de imagem paga com PostgresEffectLedger

Data: `2026-08-15`

## Resultado

Chamadas de geração de imagem do creator (OpenAI / GPT Image 2 via Vercel AI Gateway) passam a ser protegidas pelo ledger de efeitos externos (`PostgresEffectLedger`) com chave de idempotência `creator-image:{run_id}:{creator_id}:{prompt_hash}`, consumo de quota no bucket `openai_image_units`, opt-in obrigatório por `ORCH_ENABLE_PAID_ADAPTERS=true` em modo durável e classificação determinística de falhas de transporte (`ConnectTimeout` -> `failed` + liberação de quota; `ReadTimeout` e erros inesperados -> `uncertain`).

## Mudanças de contrato

- **Tool `build_creator_tool` (`src/orchestrator/tools/creators.py`):** envelopa a invocação do adapter em `execute_paid_effect` quando `is_paid_creator_adapter(ctx)` for verdadeiro.
- **Helper de detecção (`src/orchestrator/tools/base.py`):** introduz `is_paid_creator_adapter(ctx: ToolContext) -> bool` e alias `direct_creator_image_enabled`.
- **CompositeAdapter (`src/orchestrator/registry.py`):** adiciona `"image"` aos atributos delegados opcionais do papel creator.
- **Quotas operacionais (`src/orchestrator/cli.py` & `scripts/dev-local`):** adiciona `openai_image_units: 50` em `DEFAULT_DEV_QUOTAS` e ação `image-quota --units N` no script `./scripts/dev-local`.
- **Decisão canônica:** registrada em `docs/DECISIONS.md#d47--proteção-da-geração-de-imagem-paga-com-postgreseffectledger`.

## RED → GREEN

- **RED:** criação de `tests/test_paid_image_effects.py` espelhando `test_paid_voice_effects.py`, cobrindo replay idempotente, reserva e dedução de `openai_image_units`, opt-in de `ORCH_ENABLE_PAID_ADAPTERS`, obrigatoriedade de `PostgresEffectLedger`, classificação de falhas de rede (`ConnectTimeout` vs `ReadTimeout`), bypass para `MockAdapter`, validação de replay não-vazio e transição para `uncertain` em colisão de reserva. Testes falharam como esperado (9 falhas).
- **GREEN:** implementação de `is_paid_creator_adapter` em `src/orchestrator/tools/base.py`, envelopamento de `build_creator_tool` em `execute_paid_effect` em `src/orchestrator/tools/creators.py`, atualização de `DEFAULT_DEV_QUOTAS` em `src/orchestrator/cli.py` e `image-quota` em `scripts/dev-local`. Todos os 11 testes de `test_paid_image_effects.py` e os 133 testes relacionados passaram.
- **REFACTOR:** limpeza de imports não utilizados e ordenação alinhada às regras do `ruff`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `AssertionError: assert 'creator-image:...' in {}` | `build_creator_tool` invocava diretamente `ctx.adapter.build_creator` sem `execute_paid_effect`. | Envelopar a execução em `execute_paid_effect` com chave de efeito, quota `openai_image_units` e payload de request canônico quando o adapter for pago. |
| `ModuleNotFoundError: No module named 'fastapi'` | Ambiente de teste local com dependências opcionais incompletas para testes web. | Instalação de `.[dev,web]` no ambiente virtual. |
| `ValueError: contexto de tenant incompleto` em `test_cli.py` | `CLI_OFFLINE_ENV` não continha variáveis de tenant limpas pelo conftest. | Adicionar variáveis tenant mock em `CLI_OFFLINE_ENV`. |

## Verificação final

- `PYTHONPATH=. rtk proxy uv run pytest tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_creator_real.py tests/test_cli.py tests/test_dev_local.py --no-cov` — 133 testes passando sem falhas.
- `rtk proxy uv run ruff check src/ tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_creator_real.py tests/test_cli.py tests/test_dev_local.py` — 0 erros.

## Pendências ou bloqueios externos

Nenhum.
