# Validação de LatentSync Obrigatório e Persistência do Vídeo-Base em Storage Próprio

Data: `2026-08-15`

## Resultado

Implementado o suporte estrito à configuração `latentsync.required` e a persistência canônica do vídeo-base (LTX) intermediário no storage próprio (`LocalMediaStorage` / `R2MediaStorage`) e `ArtifactRepository` (`ArtifactDB`). Quando `latentsync.required=True` para talking head, a ausência de áudio ou a desativação do LatentSync resulta em falha explícita sem fallback silencioso para clipe mudo. Além disso, o artifact do clip final passa a expor `base_clip_uri` e `base_clip_source_uri`, garantindo que o vídeo-base seja persistido como `base-clip-{n}` no storage e fique imune à expiração do CDN efêmero do Replicate.

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
- `orchestrator.tools.video`:
  - `_durable_replicate_clip`: valida `latentsync_required` antes de submeter predições em talking head, levantando `VideoEffectError("latentsync_audio_missing", ...)` ou `VideoEffectError("latentsync_disabled", ...)`.
  - `_build_latentsync_artifact`: registra `meta["base_clip_uri"] = base_artifact.uri`.
- `orchestrator.media_store`:
  - `persist_item_media`: se `clip.meta.get("base_clip_uri")` estiver presente, persiste o vídeo-base sob a chave `{run_id}/items/{item_id}/base-clip-{n}`, registra o objeto como `kind="base_clip"` no `ArtifactRepository` (`db`) e atualiza os metadados do clip (`base_clip_uri`, `base_clip_source_uri`, `base_clip_storage_key`, `base_clip_storage_backend`).

## RED → GREEN

- **RED:**
  - `tests/test_latentsync_pipeline.py`: testes falhando para validação de `latentsync.required` (ausência de `audio_uri`, latentsync desabilitado e `MockAdapter`) e validação de `base_clip_uri`.
  - `tests/test_paid_video_effects.py`: testes falhando para `VideoEffectError` em modo durável quando `latentsync_required=True` e validação de `base_clip_uri`.
  - `tests/test_media_persistence_wiring.py`: teste falhando para persistência do `base_clip_uri` e registro no DB pelo `persist_item_media`.
- **GREEN:**
  - Implementação de `latentsync_required` em `ReplicateVideoAdapter`, `MockAdapter`, `CompositeAdapter` e `_durable_replicate_clip`.
  - Adição de `base_clip_uri` nos artifacts de LatentSync (`_build_latentsync_artifact`, `latentsync_artifact_from_prediction`, runner legado e mock).
  - Persistência e gravação canônica de `base_clip_uri` no `persist_item_media`.
- **REFACTOR:**
  - Integração limpa e transparente sem duplicação de chamadas de persistência de mídia.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `latentsync.required: true` no `pipeline.yaml` era ignorado quando `audio_uri` estava ausente. | `ReplicateVideoAdapter` e `_durable_replicate_clip` não liam a chave `required` e tinham condição `if audio_uri and latentsync_enabled:` retornando silenciosamente o vídeo mudo. | Armazenar `latentsync_required`, expor em `CompositeAdapter` e validar estritamente no início do fluxo levantando `RuntimeError` ou `VideoEffectError`. |
| O vídeo-base do LTX era descartado e não persistido em storage próprio, mantendo apenas URLs voláteis de CDN Replicate no ledger. | `generate_clip` e `_durable_replicate_clip` retornavam apenas o artifact com LatentSync, e `persist_item_media` só iterava sobre a URI principal dos clips. | Anexar `base_clip_uri` nos metadados do artifact do LatentSync e atualizar `persist_item_media` para baixar e persistir o `base_clip` sob chave `base-clip-{n}` no storage canônico e no `ArtifactDB`. |

## Verificação final

- `rtk proxy env PYTHONPATH=. uv run pytest tests/test_latentsync_pipeline.py tests/test_paid_video_effects.py tests/test_media_persistence_wiring.py tests/test_media_store.py tests/test_replicate_video.py tests/test_stages_coverage.py --no-cov` (138 passed)
- `rtk proxy env PYTHONPATH=. uv run pytest tests/test_cli.py --no-cov` (17 passed)

## Pendências ou bloqueios externos

Nenhum.
