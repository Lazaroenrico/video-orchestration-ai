# Validação de LatentSync Obrigatório e Persistência do Vídeo-Base em Storage Próprio

Data: `2026-08-15`

## Resultado

Implementado o suporte estrito à configuração `latentsync.required` e a persistência canônica imediata do vídeo-base (LTX) intermediário no storage próprio (`LocalMediaStorage` / `R2MediaStorage`) e `ArtifactRepository` (`ArtifactDB`) antes da conclusão do efeito durável no ledger. Quando `latentsync.required=True` para talking head, a ausência de áudio ou a desativação do LatentSync resulta em falha explícita sem fallback silencioso para clipe mudo. Além disso, o vídeo-base é baixado e persistido no storage assim que o Estágio 1 termina, garantindo que o `PostgresEffectLedger` (`external_effects`) já receba a URI canônica permanente imutável e que o Estágio 2 receba a URL acessível resolvida, tornando os replays 100% imunes à expiração do CDN efêmero do Replicate.

## Mudanças de contrato

- `ReplicateVideoAdapter`:
  - Lê e expõe `latentsync_required`.
  - Em `generate_clip`: levanta `RuntimeError` se `latentsync_required=True` e `audio_uri` estiver ausente ou `latentsync_enabled=False`.
  - Registra `meta["base_clip_uri"]` no artifact retornado após a aplicação do LatentSync.
- `MockAdapter`:
  - Aceita `latentsync` no construtor e expõe `latentsync_required` e `latentsync_enabled`.
  - Levanta `RuntimeError` se `latentsync_required=True`, `stage == "talking_head"` e `audio_uri` estiver ausente.
  - Registra `meta["base_clip_uri"]` quando o LatentSync mock é aplicado.
- `CompositeAdapter`:
  - Adicionado `"latentsync_required"` ao `_OPTIONAL_VIDEO_ATTRS`.
- `orchestrator.tools.base`:
  - `ToolContext` e `tool_context_from_config` agora propagam `storage`, `artifact_db`, `storage_resolver` e `videos_root`.
- `orchestrator.tools.video`:
  - `_durable_prediction_lifecycle`: suporta `persist_fn` assíncrono para persistir o artefato no storage antes de chamar `ledger.mark_succeeded`.
  - `_durable_replicate_clip`: persiste o vídeo-base no storage próprio antes de concluir o efeito do Estágio 1, resolve a URL acessível do vídeo-base para o provider do Estágio 2 e persiste o clipe final com a URI canônica do vídeo-base.
  - `_resolve_base_video_url_for_provider`: resolve `source_uri`, signed URLs via resolver ou data URIs locais para o provider externo.
- `orchestrator.media_store`:
  - `persist_artifact_from_url`: baixa e persiste artefatos remotos diretamente sob `{run_id}/items/{item_id}/{basename}`, atualizando metadados de storage e registrando no `ArtifactDB`.
  - `persist_item_media`: se `clip.meta.get("base_clip_uri")` estiver presente, persiste o vídeo-base sob a chave `{run_id}/items/{item_id}/base-clip-{n}`, registra o objeto como `kind="base_clip"` no `ArtifactRepository` (`db`) e atualiza os metadados do clip.

## RED → GREEN

- **RED:**
  - `tests/test_latentsync_pipeline.py`: testes falhando para validação de `latentsync.required` (ausência de `audio_uri`, latentsync desabilitado e `MockAdapter`) e validação de `base_clip_uri`.
  - `tests/test_paid_video_effects.py`: testes falhando para `VideoEffectError` em modo durável quando `latentsync_required=True`, e teste de gravação de URI canônica no ledger antes do LatentSync.
  - `tests/test_media_persistence_wiring.py`: teste falhando para persistência do `base_clip_uri` e `persist_artifact_from_url`.
- **GREEN:**
  - Implementação de `latentsync_required` em `ReplicateVideoAdapter`, `MockAdapter`, `CompositeAdapter` e `_durable_replicate_clip`.
  - Implementação de `persist_artifact_from_url` em `media_store`.
  - Persistência imediata do vídeo-base em storage canônico antes de `ledger.mark_succeeded` e resolução de URL para o Estágio 2 em `_durable_replicate_clip`.
- **REFACTOR:**
  - Ciclo de vida durável desacoplado com injeção de `persist_fn`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `latentsync.required: true` no `pipeline.yaml` era ignorado quando `audio_uri` estava ausente. | `ReplicateVideoAdapter` e `_durable_replicate_clip` não liam a chave `required` e tinham condição `if audio_uri and latentsync_enabled:` retornando silenciosamente o vídeo mudo. | Armazenar `latentsync_required`, expor em `CompositeAdapter` e validar estritamente no início do fluxo levantando `RuntimeError` ou `VideoEffectError`. |
| O vídeo-base do LTX era descartado ou concluído no ledger com URL volátil antes de persistir em storage. | `_durable_prediction_lifecycle` marcava o efeito como `succeeded` antes de salvar os bytes no storage próprio. | Injetar persistência via `persist_artifact_from_url` no ciclo de vida de predição antes de `mark_succeeded` e resolver a URL para o LatentSync. |

## Verificação final

- `rtk proxy env PYTHONPATH=. uv run pytest tests/test_latentsync_pipeline.py tests/test_paid_video_effects.py tests/test_media_persistence_wiring.py tests/test_media_store.py tests/test_replicate_video.py tests/test_stages_coverage.py tests/test_progress_docs.py tests/test_cli.py --no-cov` (145 passed)

## Pendências ou bloqueios externos

Nenhum.
