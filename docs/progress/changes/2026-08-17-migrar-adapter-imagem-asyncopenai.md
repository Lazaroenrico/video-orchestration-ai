# Migração do OpenAIImageAdapter para AsyncOpenAI

Data: `2026-08-17`

## Resultado

O adapter de geração de imagem de creators (`OpenAIImageAdapter`) foi migrado do transporte HTTP direto via `httpx` para o SDK oficial `AsyncOpenAI` (`openai>=1.0`). A implementação mantém compatibilidade total com o Vercel AI Gateway (`base_url="https://ai-gateway.vercel.sh/v1"`, `model="openai/gpt-image-2"`), preserva a conversão de `b64_json` em data URI (`data:image/png;base64,...`) e URLs diretas (`url`), suporta injeção de `AsyncOpenAI` direto, `httpx.AsyncClient` com `MockTransport` ou fakes sem rede, e garante ownership determinístico de retries via `with_transport_retry` (`max_retries=0` no SDK), preservando as chaves de efeito e persistência de mídia no `PostgresEffectLedger`.

## Mudanças de contrato

- **Adapter `OpenAIImageAdapter` (`src/orchestrator/adapters/openai_image.py`):**
  - O construtor aceita `client: Optional[AsyncOpenAI | httpx.AsyncClient | Any] = None`. Se um `AsyncOpenAI` for fornecido, ele é usado diretamente. Se um `httpx.AsyncClient` for injetado, ele é envelopado via `AsyncOpenAI(http_client=client, max_retries=0)`. Se `None`, o adapter instancia o client `AsyncOpenAI` por chamada (`async with httpx.AsyncClient(timeout=...) as http_client:`).
  - As chamadas usam `client.images.generate(model=self.model, prompt=prompt)` e extraem `url` ou `b64_json`.
  - Falhas de status do gateway levantam `openai.APIStatusError` (ex.: `BadRequestError`, `AuthenticationError`, `RateLimitError`) preservando o corpo da resposta em logs e metadados de span de tracing (`image_error_status`, `image_error_body`).
- **Retry e classificação de falhas (`src/orchestrator/adapters/_retry.py` & `src/orchestrator/tools/base.py`):**
  - `_is_retryable` e `_definitely_not_billed` suportam exceções do SDK OpenAI com `status_code == 429` e erros de conexão pré-envio envelopados em `exc.__cause__`.

## RED → GREEN

- **RED:** Atualização e criação de testes em `tests/test_creator_real.py` caracterizando a injeção de `AsyncOpenAI`, fake clients sem rede, propagação de `openai.APIStatusError` em 4xx e retry em 429 RateLimitError.
- **GREEN:** Refatoração de `OpenAIImageAdapter` para utilizar `AsyncOpenAI`, configuração de `max_retries=0` no SDK, ajuste de `_is_retryable` e `_definitely_not_billed` para suportar `status_code` e `__cause__`. Todos os 82 testes de `test_creator_real.py` e `test_paid_image_effects.py` e 326 testes da suíte passaram.
- **REFACTOR:** Ordenação de imports com `ruff check` e padronização do patch helper `_patch_own_client` como subclasse de `httpx.AsyncClient`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `openai.RateLimitError` propagava sem retry em `with_transport_retry` | `_is_retryable` em `_retry.py` verificava apenas `httpx.HTTPStatusError` e `ReplicateError`, ignorando `openai.APIStatusError` / `status_code == 429`. | Adicionar verificação `getattr(exc, "status_code", None) == 429` e `exc.__cause__ in _PRE_SEND_TRANSPORT_ERRORS` em `_is_retryable`. |
| `TypeError: isinstance() arg 2 must be a type...` em `test_openai_generate_face_uses_own_client_and_gender_clause` | `_patch_own_client` substituía `module.httpx.AsyncClient` por uma função `lambda`, quebrando a checagem interna `isinstance(value, module.AsyncClient)` do SDK `openai`. | Definir `_PatchedAsyncClient(httpx.AsyncClient)` como subclasse em `_patch_own_client`. |
| `F821 Undefined name Any` e `I001 Import block is un-sorted` no `ruff check` | Faltava import `from typing import Any` e ordem de imports de terceiros desajustada em `tests/test_creator_real.py`. | Importar `Any` de `typing` e ordenar imports de acordo com as regras do `ruff`. |

## Verificação final

- `rtk proxy uv run pytest tests/test_creator_real.py tests/test_paid_image_effects.py tests/test_retry.py tests/test_tools.py tests/test_creative_agent_tools.py tests/test_stages_coverage.py tests/test_runtime_contract.py tests/test_runtime_contract_resume.py tests/test_registry_composite.py tests/test_graph_e2e.py tests/test_cli.py tests/test_dev_local.py tests/test_tracing_coverage.py tests/test_media_store.py tests/test_media_persistence_wiring.py --no-cov` — 326 testes passando com sucesso.
- `rtk proxy uv run ruff check src/ tests/` — 0 erros.

## Pendências ou bloqueios externos

Nenhum.
