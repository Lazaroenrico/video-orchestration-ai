# Integração LatentSync 2-Estágios no Talking Head (LTX + ElevenLabs -> LatentSync 720p)

Data: `2026-08-07`

## Resultado

Implementada a pipeline de *talking head* em 2 estágios (`LTX` -> `LatentSync` via Replicate) em resolução 720p com política de até 3 retentativas idempotentes e sem fallback silencioso para vídeo silencioso em produção.

## Mudanças de contrato

- `config/pipeline.yaml`: adicionadas configurações `clip.resolution: "720p"` e a seção `latentsync` (`enabled: true`, `model: "bytedance/latentsync"`, `max_retries: 3`, `required: true`).
- `ReplicateVideoAdapter`: encadeia o modelo `bytedance/latentsync` quando `audio_uri` está presente na chamada do clipe de *talking head*. Em caso de falha após 3 tentativas, lança exceção explícita.

## RED → GREEN

- **RED:** `tests/test_latentsync_pipeline.py` cobrindo o encadeamento LTX->LatentSync, resolução 720p, retentativas 3x e rejeição de fallback silencioso.
- **GREEN:** Implementação no `ReplicateVideoAdapter`, `MockAdapter` e `config/pipeline.yaml`.
- **REFACTOR:** Limpeza e validação de metadata dos artifacts gerados.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| N/A | N/A | N/A |

## Verificação final

- `rtk proxy python -m pytest tests/test_latentsync_pipeline.py`
- `rtk proxy python -m pytest tests/test_replicate_video.py`

## Pendências ou bloqueios externos

Nenhum.
