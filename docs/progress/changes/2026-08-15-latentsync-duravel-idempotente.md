# LatentSync durável, idempotente e reconciliável (D45)

Data: `2026-08-15`

## Resultado

Estendido o modelo durável do Replicate (D45) para a execução do LatentSync em 2 estágios (`LTX` -> `LatentSync`).
Cada estágio possui reserva durável e idempotente dedicada no ledger (`video:...` e `latentsync:...`). Falhas de transporte pós-envio (`WriteTimeout`, `ReadTimeout`) são reconciliadas via webhook e polling sem repetição de POST ou cobrança duplicada. Em caso de crash ou reexecução após a conclusão do estágio base, o artifact do vídeo base é reaproveitado sem re-geração. Falhas definitivas no LatentSync cancelam a prediction no provider e lançam `VideoEffectError` estruturado sem fallback silencioso para vídeo mudo.

## Mudanças de contrato

- `ReplicateVideoAdapter`:
  - Expõe `submit_latentsync_prediction(video_uri, audio_uri, resolution, webhook_url)` e `latentsync_artifact_from_prediction(prediction, base_artifact)`.
  - Executa o ciclo de vida completo de predições (submit -> poll -> output) para o LatentSync quando `self._runner is None` e `audio_uri` está presente.
- `src/orchestrator/tools/video.py`:
  - `generate_clip_tool` repassa `audio_uri` para `_durable_replicate_clip`.
  - `_durable_replicate_clip` divide a geração em dois efeitos duráveis determinísticos:
    - Estágio 1: `video:{run_id}:{item_id}:{stage}:{attempt}:{request_hash}` (vídeo base LTX/Pruna).
    - Estágio 2: `latentsync:{run_id}:{item_id}:{stage}:{attempt}:{request_hash}` (lip-sync LatentSync).
  - Ambos os estágios tratam erros pré-envio com retry, erros ambíguos pós-envio via `wait_for_provider_operation`, timeout com cancelamento explícito no provider (`cancel_video_prediction`) e retorno do artifact com metadados `latentsync_applied=True`, `latentsync_model` e `prediction_id`.
- `CompositeAdapter`:
  - Adicionados `submit_latentsync_prediction`, `latentsync_artifact_from_prediction`, `latentsync_enabled`, `latentsync_model`, `latentsync_resolution`, `latentsync_max_retries` aos `_OPTIONAL_VIDEO_ATTRS`.
- `_build_replicate` em `registry.py`:
  - Repassa a seção `latentsync` de `pipeline.yaml` para o `ReplicateVideoAdapter`.

## RED → GREEN

- **RED:**
  - `tests/test_latentsync_pipeline.py`: Adicionados testes para o ciclo de predições do LatentSync com `self._runner is None` e validação de metadata/rejeição de falhas.
  - `tests/test_paid_video_effects.py`: Adicionados testes de pipeline durável em 2 estágios, recuperação após crash (reutilização do vídeo base LTX), reconciliação de `WriteTimeout` no LatentSync sem POST duplicado, cancelamento de timeout e propagação estrita de erro sem fallback silencioso.
- **GREEN:**
  - Implementação de `submit_latentsync_prediction` e `latentsync_artifact_from_prediction` no `ReplicateVideoAdapter`.
  - Implementação da máquina de estados `_durable_prediction_lifecycle` e orquestração de 2 estágios em `src/orchestrator/tools/video.py`.
  - Atualização dos atributos opcionais em `CompositeAdapter` e registro do adapter Replicate.
- **REFACTOR:**
  - Extração do lifecycle durável comum (`_durable_prediction_lifecycle`) para eliminar duplicação de lógica entre Stage 1 (vídeo base) e Stage 2 (LatentSync).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Ao rodar LatentSync em modo durável, apenas o vídeo base LTX era executado de forma durável, ignorando o áudio no caminho durável. | `generate_clip_tool` não passava `audio_uri` para `_durable_replicate_clip`, e `_durable_replicate_clip` não continha o segundo estágio de efeito durável. | Adicionar parâmetro `audio_uri` em `_durable_replicate_clip`, criar reserva de efeito dedicada `latentsync:...` e orquestrar o lifecycle do LatentSync. |
| `CompositeAdapter` levantava `AttributeError` ao tentar acessar atributos do LatentSync no adapter de vídeo. | Atributos e métodos do LatentSync não estavam em `_OPTIONAL_VIDEO_ATTRS`. | Registrar `submit_latentsync_prediction`, `latentsync_artifact_from_prediction`, `latentsync_enabled`, `latentsync_model`, `latentsync_resolution`, `latentsync_max_retries` em `_OPTIONAL_VIDEO_ATTRS`. |

## Verificação final

- Testes unitários e de integração cobrindo:
  1. Execução LTX -> LatentSync em 2 estágios com predições Replicate.
  2. Idempotência / recuperação após crash: LTX concluído faz replay sem novo POST e executa apenas LatentSync.
  3. Reconciliação em `WriteTimeout` no LatentSync (2 tentativas iniciais e nenhuma 3ª, resolvido via webhook/polling).
  4. Cancelamento de predição lenta ao atingir timeout no Replicate.
  5. Falha no LatentSync levantando `VideoEffectError` sem fallback silencioso para clipe mudo.

## Pendências ou bloqueios externos

Nenhum.
