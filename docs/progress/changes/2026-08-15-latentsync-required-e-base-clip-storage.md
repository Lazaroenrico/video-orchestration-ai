# Validação de LatentSync Obrigatório e Persistência do Vídeo-Base em Storage Próprio

Data: `2026-08-15`

## Resultado

Implementado o suporte estrito à configuração `latentsync.required` e a persistência canônica imediata do vídeo-base (LTX) intermediário no storage próprio (`LocalMediaStorage` / `R2MediaStorage`) e `ArtifactRepository` (`ArtifactDB`) antes da conclusão do efeito durável no ledger. Quando `latentsync.required=True` para talking head, a ausência de áudio ou a desativação do LatentSync resulta em falha explícita sem fallback silencioso para clipe mudo. Além disso, o vídeo-base é baixado e persistido no storage assim que o Estágio 1 termina, garantindo que o `PostgresEffectLedger` (`external_effects`) já receba a URI canônica permanente imutável e que o Estágio 2 receba a URL acessível resolvida, tornando os replays 100% imunes à expiração do CDN efêmero do Replicate.

## Mudanças de contrato

- `ReplicateVideoAdapter`:
  - Lê e expõe `latentsync_required` e `latentsync_cost_per_second`.
  - Em `generate_clip`: aceita `stage`, valida `latentsync_required` para `stage != "product_demo"`, levanta `RuntimeError` se `audio_uri` estiver ausente ou `latentsync_enabled=False`, e repassa `stage` para o fallback mock.
  - Adiciona `latentsync_cost_usd` e soma o custo do LatentSync em `meta["cost_usd"]`.
  - Registra `meta["base_clip_uri"]` no artifact retornado após a aplicação do LatentSync.
- `MockAdapter`:
  - Aceita `latentsync` no construtor e expõe `latentsync_required`, `latentsync_enabled` e `latentsync_cost_per_second`.
  - Em `generate_clip`: aceita `stage`, valida `latentsync_required` para `stage != "product_demo"`, e soma `latentsync_cost_usd` em `meta["cost_usd"]`.
  - Registra `meta["base_clip_uri"]` quando o LatentSync mock é aplicado.
- `CompositeAdapter`:
  - Adicionados `"latentsync_required"` e `"latentsync_cost_per_second"` ao `_OPTIONAL_VIDEO_ATTRS`.
- `orchestrator.tools.base`:
  - `ToolContext` e `tool_context_from_config` agora propagam `storage`, `artifact_db`, `storage_resolver` e `videos_root`.
- `orchestrator.tools.video`:
  - `_durable_prediction_lifecycle`: suporta `persist_fn` assíncrono para persistir o artefato no storage antes de chamar `ledger.mark_succeeded`.
  - `_durable_replicate_clip`: persiste o vídeo-base no storage próprio antes de concluir o efeito do Estágio 1, resolve a URL acessível do vídeo-base para o provider do Estágio 2 e persiste o clipe final com a URI canônica do vídeo-base.
  - `_resolve_base_video_url_for_provider`: prioriza a resolução de signed URLs via `storage_resolver` e dados locais antes de qualquer fallback de `source_uri`, garantindo que replays nunca quebrem por URLs expiradas do Replicate CDN.
  - `_build_latentsync_artifact`: soma `latentsync_cost_usd` em `cost_usd`.
  - `generate_clip_tool`: inspeciona a assinatura de `ctx.adapter.generate_clip` e passa `stage` somente se aceito, com fallback transparente para evitar quebras em implementações estritas do `VideoPort`.
- `orchestrator.media_store`:
  - `persist_artifact_from_url`: baixa e persiste artefatos remotos diretamente sob `{run_id}/items/{item_id}/{basename}`, atualizando metadados de storage e registrando no `ArtifactDB`.
  - `persist_item_media`: se `clip.meta.get("base_clip_uri")` estiver presente, persiste o vídeo-base sob a chave `{run_id}/items/{item_id}/base-clip-{n}`, registra o objeto como `kind="base_clip"` no `ArtifactRepository` (`db`) e atualiza os metadados do clip.

## RED → GREEN

- **RED:**
  - `tests/test_latentsync_pipeline.py`: testes falhando para validação de `latentsync.required` (ausência de `audio_uri`, latentsync desabilitado, `MockAdapter`, e isenção de `product_demo`), cômputo de custo de LatentSync e validação de `base_clip_uri`.
  - `tests/test_paid_video_effects.py`: testes falhando para `VideoEffectError` em modo durável quando `latentsync_required=True`, gravação de URI canônica no ledger antes do LatentSync, e resolução de signed URLs em replay com `source_uri` expirada.
  - `tests/test_media_persistence_wiring.py`: teste falhando para persistência do `base_clip_uri` e `persist_artifact_from_url`.
  - `tests/test_system_prompt.py`, `tests/test_resume_partial.py`, `tests/test_tracing.py`: falhas de `TypeError` por passagem incondicional de `stage` para adapters que seguem estritamente o `VideoPort`.
- **GREEN:**
  - Implementação de `latentsync_required` com isenção de `product_demo` em `ReplicateVideoAdapter`, `MockAdapter`, `CompositeAdapter` e `_durable_replicate_clip`.
  - Implementação de cômputo de custo `latentsync_cost_per_second` somado em `cost_usd`.
  - Implementação de `persist_artifact_from_url` em `media_store`.
  - Resolução segura de signed URLs em `_resolve_base_video_url_for_provider` sem depender de `source_uri` expirada no replay.
  - Compatibilidade retroativa garantida em `generate_clip_tool` via `inspect.signature` e fallback de `TypeError`.
  - Remoção de arquivos fora de escopo (`senior-implemater.md`).
- **REFACTOR:**
  - Ciclo de vida durável desacoplado com injeção de `persist_fn` e ordenação de imports validada pelo ruff.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `latentsync.required: true` no `pipeline.yaml` era ignorado quando `audio_uri` estava ausente. | `ReplicateVideoAdapter` e `_durable_replicate_clip` não liam a chave `required` e tinham condição `if audio_uri and latentsync_enabled:` retornando silenciosamente o vídeo mudo. | Armazenar `latentsync_required`, expor em `CompositeAdapter` e validar estritamente no início do fluxo levantando `RuntimeError` ou `VideoEffectError` quando `stage != "product_demo"`. |
| O vídeo-base do LTX era descartado ou concluído no ledger com URL volátil antes de persistir em storage. | `_durable_prediction_lifecycle` marcava o efeito como `succeeded` antes de salvar os bytes no storage próprio. | Injetar persistência via `persist_artifact_from_url` no ciclo de vida de predição antes de `mark_succeeded` e resolver a URL para o LatentSync. |
| Replay de LatentSync tentava usar URL efêmera do Replicate expirada em `source_uri`. | `_resolve_base_video_url_for_provider` priorizava `source_uri` antes de `storage_resolver`. | Inverter a precedência para resolver via `storage_resolver` / signed URL antes de qualquer fallback para `source_uri`. |
| Custo configurado de LatentSync (`cost_per_second`) não entrava no custo do item/clip. | `_build_latentsync_artifact`, `latentsync_artifact_from_prediction` e `MockAdapter` mantinham apenas o `cost_usd` do vídeo base. | Calcular `latentsync_cost_usd` e somá-lo em `meta["cost_usd"]`. |
| `TypeError: generate_clip() got an unexpected keyword argument 'stage'` em adapters que implementam estritamente o `VideoPort`. | `generate_clip_tool` passava `stage=stage` incondicionalmente no caminho não-durável. | Inspecionar a assinatura via `inspect.signature` e repassar `stage` apenas quando aceito pelo adapter, com fallback seguro para `TypeError`. |

## Verificação final

- `rtk proxy env PYTHONPATH=. uv run pytest tests/test_tools.py tests/test_resume_partial.py tests/test_system_prompt.py tests/test_tracing.py tests/test_latentsync_pipeline.py tests/test_paid_video_effects.py tests/test_media_persistence_wiring.py tests/test_media_store.py tests/test_replicate_video.py tests/test_stages_coverage.py tests/test_progress_docs.py tests/test_cli.py --no-cov` (244 passed)
- `rtk proxy uv run ruff check src tests` (0 errors)

## Pendências ou bloqueios externos

Nenhum.
