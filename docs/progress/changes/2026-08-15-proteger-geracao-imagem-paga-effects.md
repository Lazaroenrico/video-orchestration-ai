# Proteção da geração de imagem paga com PostgresEffectLedger

Data: `2026-08-15`

## Resultado

Chamadas de geração de imagem do creator (OpenAI / GPT Image 2 via Vercel AI Gateway) passam a ser protegidas pelo ledger de efeitos externos (`PostgresEffectLedger`) com chave de idempotência `creator-image:{run_id}:{creator_id}:{prompt_hash}`, consumo de quota no bucket `openai_image_units`, opt-in obrigatório por `ORCH_ENABLE_PAID_ADAPTERS=true` em modo durável e classificação determinística de falhas de transporte (`ConnectTimeout` -> `failed` + liberação de quota; `ReadTimeout` e erros inesperados -> `uncertain`).

A persistência canônica de mídia (`media_store.persist_creator_media`) foi movida para **dentro** da operação protegida pelo ledger (`_build` em `build_creator_tool`). O ledger grava em `external_effects.result` o objeto já com as URIs canônicas (`r2://...` ou `/media/...`), eliminando URLs efêmeras da OpenAI (expiração em 1h) e base64 volumoso do banco de dados (ADR-D30, D45, D47).

## Mudanças de contrato

- **Tool `build_creator_tool` (`src/orchestrator/tools/creators.py`):** aceita parâmetros opcionais de persistência `media_root: Optional[str | Path] = None`, `storage: Optional[Any] = None` e `db: Optional[Any] = None`. A persistência canônica via `media_store.persist_creator_media` é executada dentro de `_build` antes do retorno ao `execute_paid_effect`, garantindo que o ledger grave apenas ponteiros canônicos persistidos (`r2://...` ou `/media/...`).
- **Node `node_roster` (`src/orchestrator/nodes/stages.py`):** repassa `media_root` e os kwargs de `_persistence(config, storage_key="media_storage")` para `execute_stage_tool(..., tool_fn=build_creator_tool, ...)`.
- **Helper de detecção (`src/orchestrator/tools/base.py`):** introduz `is_paid_creator_adapter(ctx: ToolContext) -> bool` e alias `direct_creator_image_enabled`.
- **CompositeAdapter (`src/orchestrator/registry.py`):** adiciona `"image"` aos atributos delegados opcionais do papel creator.
- **Quotas operacionais (`src/orchestrator/cli.py` & `scripts/dev-local`):** adiciona `openai_image_units: 50` em `DEFAULT_DEV_QUOTAS` e ação `image-quota --units N` no script `./scripts/dev-local`.
- **Decisão canônica:** registrada e atualizada em `docs/DECISIONS.md#d47--proteção-da-geração-de-imagem-paga-com-postgreseffectledger`.

## RED → GREEN

- **RED:** criação de `tests/test_paid_image_effects.py` espelhando `test_paid_voice_effects.py`, cobrindo replay idempotente, reserva e dedução de `openai_image_units`, opt-in de `ORCH_ENABLE_PAID_ADAPTERS`, obrigatoriedade de `PostgresEffectLedger`, classificação de falhas de rede (`ConnectTimeout` vs `ReadTimeout`), bypass para `MockAdapter`, validação de replay não-vazio e transição para `uncertain` em colisão de reserva. Testes estendidos para persistência canônica em R2 e falhas antes de `mark_succeeded` falharam com `TypeError` de argumentos não aceitos por `build_creator_tool`.
- **GREEN:** implementação de `is_paid_creator_adapter` em `src/orchestrator/tools/base.py`, envelopamento de `build_creator_tool` em `execute_paid_effect` com persistência canônica embutida em `_build` (`src/orchestrator/tools/creators.py`), propagação de storage/db/media_root em `node_roster` (`src/orchestrator/nodes/stages.py`), atualização de `DEFAULT_DEV_QUOTAS` em `src/orchestrator/cli.py` e `image-quota` em `scripts/dev-local`. Todos os 13 testes de `test_paid_image_effects.py` e todos os testes da suíte passaram.
- **REFACTOR:** limpeza de imports, ordenação alinhada às regras do `ruff` e compatibilidade total offline/mock.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `AssertionError: assert 'creator-image:...' in {}` | `build_creator_tool` invocava diretamente `ctx.adapter.build_creator` sem `execute_paid_effect`. | Envelopar a execução em `execute_paid_effect` com chave de efeito, quota `openai_image_units` e payload de request canônico quando o adapter for pago. |
| `TypeError: build_creator_tool() got an unexpected keyword argument 'media_root'/'storage'` | `build_creator_tool` não recebia nem repassava parâmetros de storage para persistência canônica interna. | Adicionar `media_root`, `storage` e `db` na assinatura de `build_creator_tool` e acionar `media_store.persist_creator_media` dentro de `_build`. |
| `ModuleNotFoundError: No module named 'fastapi'` | Ambiente de teste local com dependências opcionais incompletas para testes web. | Instalação de `.[dev,web]` no ambiente virtual. |
| `ValueError: contexto de tenant incompleto` em `test_cli.py` | `CLI_OFFLINE_ENV` não continha variáveis de tenant limpas pelo conftest. | Adicionar variáveis tenant mock em `CLI_OFFLINE_ENV`. |

## Verificação final

- `PYTHONPATH=. rtk proxy uv run pytest tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_creator_real.py tests/test_media_store.py tests/test_media_persistence_wiring.py --no-cov` — 127 testes passando sem falhas.
- `rtk proxy uv run ruff check src/ tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_creator_real.py tests/test_media_store.py tests/test_media_persistence_wiring.py` — 0 erros.

## Pendências ou bloqueios externos

Nenhum.
