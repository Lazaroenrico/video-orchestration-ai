# Proteção da geração de imagem paga com PostgresEffectLedger

Data: `2026-08-15`

## Resultado

Chamadas de geração de imagem do creator (OpenAI / GPT Image 2 via Vercel AI Gateway) passam a ser protegidas pelo ledger de efeitos externos (`PostgresEffectLedger`) com chave de idempotência `creator-image:{run_id}:{creator_id}:{model_slug}:{prompt_hash}`, consumo de quota no bucket `openai_image_units`, opt-in obrigatório por `ORCH_ENABLE_PAID_ADAPTERS=true` em modo durável e classificação determinística de falhas de transporte (`ConnectTimeout` -> `failed` + liberação de quota; `ReadTimeout` e erros inesperados -> `uncertain`).

A persistência canônica de mídia (`media_store.persist_creator_media`) foi movida para **dentro** da operação protegida pelo ledger (`_build` em `build_creator_tool`). O dict retornado por `_build` é sanitizado para remover `image_source_uri` quando este contiver URL efêmera ou payload de data URI base64, garantindo que o ledger grave em `external_effects.result` apenas ponteiros canônicos (`r2://...` ou `/media/...`) e metadados leves (`id`, `angles`, `voice_id`, etc.), eliminando URLs efêmeras da OpenAI (expiração em 1h) e base64 volumoso do banco de dados (ADR-D30, D45, D47).

## Mudanças de contrato

- **Tool `build_creator_tool` (`src/orchestrator/tools/creators.py`):** aceita parâmetros opcionais de persistência `media_root: Optional[str | Path] = None`, `storage: Optional[Any] = None` e `db: Optional[Any] = None`. A persistência canônica via `media_store.persist_creator_media` é executada dentro de `_build` antes do retorno ao `execute_paid_effect`, garantindo que o ledger grave apenas ponteiros canônicos persistidos (`r2://...` ou `/media/...`). Inclui validação estrita pós-persistência que levanta `RuntimeError` caso `image_source_uri` esteja ausente ou `upscaled_base` ainda seja uma URI baixável/efêmera (evitando `mark_succeeded` silencioso). O dict de resultado é sanitizado para descarregar `image_source_uri` de URLs efêmeras/data URIs antes da persistência no ledger. A chave de efeito inclui o slug do modelo (`model_slug = model.replace("/", "_").replace(":", "_").replace(".", "_")`).
- **Node `node_roster` (`src/orchestrator/nodes/stages.py`):** repassa `media_root` e os kwargs de `_persistence(config, storage_key="media_storage")` para `execute_stage_tool(..., tool_fn=build_creator_tool, ...)`.
- **Helper de detecção (`src/orchestrator/tools/base.py`):** introduz `is_paid_creator_adapter(ctx: ToolContext) -> bool` e alias `direct_creator_image_enabled`.
- **CompositeAdapter (`src/orchestrator/registry.py`):** adiciona `"image"` aos atributos delegados opcionais do papel creator.
- **Quotas operacionais (`src/orchestrator/cli.py` & `scripts/dev-local`):** adiciona `openai_image_units: 50` em `DEFAULT_DEV_QUOTAS` e ação `image-quota --units N` no script `./scripts/dev-local`.
- **Decisão canônica:** registrada e atualizada em `docs/DECISIONS.md#d47--proteção-da-geração-de-imagem-paga-com-postgreseffectledger`.

## RED → GREEN

- **RED:** criação de `tests/test_paid_image_effects.py` espelhando `test_paid_voice_effects.py`, cobrindo replay idempotente, reserva e dedução de `openai_image_units`, opt-in de `ORCH_ENABLE_PAID_ADAPTERS`, obrigatoriedade de `PostgresEffectLedger`, classificação de falhas de rede (`ConnectTimeout` vs `ReadTimeout`), bypass para `MockAdapter`, validação de replay não-vazio, transição para `uncertain` em colisão de reserva e replay de efeito com falha prévia (`status=failed` transita para `uncertain`). Testes estendidos para persistência canônica em R2, ausência de `image_source_uri` no ledger e falhas antes de `mark_succeeded`.
- **GREEN:** implementação de `is_paid_creator_adapter` em `src/orchestrator/tools/base.py`, envelopamento de `build_creator_tool` em `execute_paid_effect` com chave de efeito com `model_slug`, persistência canônica embutida em `_build` (`src/orchestrator/tools/creators.py`), sanitização de `image_source_uri` no resultado gravado no ledger, validação estrita que levanta `RuntimeError` para imagens não persistidas, propagação de storage/db/media_root em `node_roster` (`src/orchestrator/nodes/stages.py`), atualização de `DEFAULT_DEV_QUOTAS` em `src/orchestrator/cli.py` e `image-quota` em `scripts/dev-local`. Todos os 15 testes de `test_paid_image_effects.py` e todos os 129 testes da suíte passaram.
- **REFACTOR:** limpeza de imports, ordenação alinhada às regras do `ruff`, remoção de arquivos fora de escopo em `.agents/agents/` e compatibilidade total offline/mock.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `AssertionError: assert 'creator-image:...' in {}` | `build_creator_tool` invocava diretamente `ctx.adapter.build_creator` sem `execute_paid_effect`. | Envelopar a execução em `execute_paid_effect` com chave de efeito, quota `openai_image_units` e payload de request canônico quando o adapter for pago. |
| `TypeError: build_creator_tool() got an unexpected keyword argument 'media_root'/'storage'` | `build_creator_tool` não recebia nem repassava parâmetros de storage para persistência canônica interna. | Adicionar `media_root`, `storage` e `db` na assinatura de `build_creator_tool` e acionar `media_store.persist_creator_media` dentro de `_build`. |
| `Failed: DID NOT RAISE RuntimeError` com payload efêmero gravado como succeeded no ledger | `persist_creator_media` retornava dict inalterado quando `put_from_url` retornava `None` (best-effort), permitindo `mark_succeeded` com URL efêmera/base64. | Adicionar validação estrita em `build_creator_tool` para adapters pagos levantando `RuntimeError` se `image_source_uri` não for populada ou `upscaled_base` permanecer baixável, marcando o ledger como `uncertain`. |
| `AssertionError: assert 'openai_gpt-image-2' in effect_key` | Chave de efeito usava `creator-image:{run_id}:{creator_id}:{prompt_hash}` sem incluir o modelo. | Atualizar a geração da chave para incluir `model_slug = model.replace("/", "_").replace(":", "_").replace(".", "_")`. |
| Payload volumoso / efêmero gravado em `external_effects.result` | `_build` retornava `image_source_uri` com data URI base64 / URL temporária da OpenAI. | Sanitizar o dict retornado por `_build` removendo `image_source_uri` se for downloadable/data URI antes de entregar a `execute_paid_effect`. |
| `ModuleNotFoundError: No module named 'fastapi'` | Ambiente de teste local com dependências opcionais incompletas para testes web. | Instalação de `.[dev,web]` no ambiente virtual. |
| `ValueError: contexto de tenant incompleto` em `test_cli.py` | `CLI_OFFLINE_ENV` não continha variáveis de tenant limpas pelo conftest. | Adicionar variáveis tenant mock em `CLI_OFFLINE_ENV`. |

## Verificação final

- `PYTHONPATH=. rtk proxy uv run pytest tests/test_paid_image_effects.py tests/test_paid_voice_effects.py tests/test_paid_video_effects.py tests/test_creator_real.py tests/test_media_store.py tests/test_media_persistence_wiring.py --no-cov` — 129 testes passando sem falhas.
- `rtk proxy uv run ruff check src/ tests/` — 0 erros.

## Pendências ou bloqueios externos

Nenhum.

