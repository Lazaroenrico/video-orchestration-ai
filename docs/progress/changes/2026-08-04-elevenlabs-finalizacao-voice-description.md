# Finalização de voz compatível com o contrato ElevenLabs

Data: `2026-08-04`

## Resultado

A finalização de uma voz escolhida agora envia uma descrição server-owned de 37
caracteres, dentro do limite de 20–1000 caracteres do provider. O fluxo deixa de falhar
com 422 antes de receber o `voice_id` permanente.

## Mudanças de contrato

Nenhuma. API HTTP, schemas, banco, configuração YAML e política de logs permanecem
inalterados. O checkpoint continua guardando somente `description_hash`, sem transportar
ou persistir a descrição criativa original.

## RED → GREEN

- **RED:** `test_elevenlabs_voice_design_adapter_calls_create_endpoint` passou a usar um
  `MockTransport` que aplica o limite do provider. A chamada pública `finalize_voice`
  recebeu `422 Unprocessable Entity` porque `"UGC creator voice"` tinha 17 caracteres.
- **GREEN:** o adapter passou a enviar a constante server-owned
  `"Synthetic voice for an AI UGC creator"`, com 37 caracteres; os 36 testes do adapter
  passaram sem alterar o tratamento de erro.
- **REFACTOR:** não aplicável; a correção ficou isolada no payload de finalização.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `POST /v1/text-to-voice` retornava 422 e o run live parava antes da produção. | A descrição fixa de finalização tinha 17 caracteres, abaixo do mínimo de 20 aceito pelo ElevenLabs. | Substituição por uma descrição server-owned de 37 caracteres e regressão no transporte HTTP. |
| A primeira suíte completa registrou 115 erros de setup e cobertura parcial. | Não havia PostgreSQL de testes em `127.0.0.1:5432`; nenhum dos erros alcançou o código sob teste. | Um PostgreSQL 16 temporário e isolado foi iniciado para a suíte e removido depois da validação. |
| O teste de erro do FFmpeg recebeu `timed out` em vez de `failed: expected-error` somente sob carga da suíte. | O mesmo adapter com timeout de 10 ms validava tanto o timeout intencional quanto um processo curto que precisava concluir com exit 2. | O timeout normal passou a validar stderr/exit 2; o adapter de 10 ms ficou restrito ao caso `sleep 1`, sem alterar asserções. |
| O run live novo parou ao iniciar o primeiro clip, depois de criar as duas vozes permanentes. | O checkpoint do subgrafo registrou `WriteTimeout('')` no node `pruna`; por ser timeout pós-envio ambíguo, a política durável não repete a chamada. | Nenhum novo retry pago foi emitido. A falha do provider de vídeo ficou isolada da correção de voz para diagnóstico operacional separado. |

## Verificação final

- RED reproduzido com 422, `status` e `request_id`, sem corpo do provider no log.
- `tests/test_elevenlabs_voice_design.py --no-cov`: 36 testes passaram.
- Testes documentais: 5 passaram; Ruff e `git diff --check`: passaram.
- Suíte completa com PostgreSQL 16 isolado: 1419 passaram, 2 skips live esperados e
  cobertura de 100%; o container temporário foi removido.
- Stack live local rebuildado e `/readyz` retornou `ready` com R2. Antes do retry, as
  quotas estavam em `524/100000` caracteres de design, `0/20` slots e `0/500000`
  caracteres de TTS, acima da folga requerida.
- Foi feito exatamente um retry de `web-7d231c9f`, criando `web-7e526a1b`; o run antigo
  permaneceu em `error`.
- Os dois `POST /v1/text-to-voice` retornaram 200. O ledger marcou ambas as finalizações
  como `succeeded`, e o checkpoint persistiu um `voice_ref` permanente e
  `voice_status=selected` para cada creator.
- O run avançou para Produção e QC, mas parou no primeiro node `pruna` por
  `WriteTimeout('')`; TTS e montagem não foram alcançados.

## Pendências ou bloqueios externos

- Diagnosticar a conectividade/timeout de escrita do Replicate Pruna antes de autorizar
  outra campanha paga. Não repetir `web-7e526a1b`; qualquer retry manual futuro precisa
  criar um novo `web-...`.
