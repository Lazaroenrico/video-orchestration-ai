# Prediction Replicate durável e falha parcial por item

Data: `2026-08-04`

## Resultado

Timeout pós-envio do Replicate deixa de repetir uma prediction potencialmente cobrada e
deixa de encerrar o lote inteiro. Prediction ID/status são reconciliados por webhook e
polling; falha esperada fica estruturada no item enquanto os demais itens continuam.

## Mudanças de contrato

- `Item.failure` expõe código, tipo, estágio, provider, effect key e flags de retry/
  incerteza; `Item.error` continua não vazio para compatibilidade.
- `external_effects`, `jobs` e `runs` passam pela migration `20260804_0011` descrita em
  [D45](../../DECISIONS.md#d45--prediction-replicate-durável-reconciliável-e-isolada-por-item).
- O webhook público e o procedimento de quota/configuração estão no
  [runbook operacional](../../OPERATIONS.md#replicate-live-durável).

## RED → GREEN

- **RED:** o tracer `ConnectError → WriteTimeout` falhou porque o adapter não expunha
  cliente de prediction e a tool não tinha ledger intermediário; o ID não podia ser
  reconciliado.
- **GREEN:** criação repete apenas a falha pré-envio, persiste/reconcilia o ID e consulta
  a mesma prediction; o teste confirma duas tentativas de POST e nenhuma terceira.
- **REFACTOR:** lifecycle explícito ficou no adapter, decisão idempotente/ledger na tool e
  autenticação monotônica num módulo de webhook reutilizado pela API.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Logava `replicate.video falhou (1/4): ConnectError` e o job encerrava depois. | A primeira falha era retentável; a segunda era `WriteTimeout('')`, ambígua e sem ID persistido. | Separar create/get/cancel, reconciliar por webhook e nunca repetir timeout pós-envio. |
| Erro persistido podia ser vazio. | `str(httpx.WriteTimeout("")) == ""`. | Persistir mensagem fallback e `error_type=WriteTimeout`. |
| Um item interrompia todo o fan-out. | Exceção do subgrafo propagava ao `process_item`. | Capturar somente `VideoEffectError`, persistir `FailureDetail` e rotear esse item a `END`. |
| UI dizia “Assembly Failed” para qualquer erro. | Estágio era inferido por texto/fallback fixo. | Contrato público mantém `failure.stage`; front e progresso usam o estágio real. |
| Teste PostgreSQL não iniciou no endpoint padrão; ao apontar para o container em `55432`, o fixture falhou ao criar o banco temporário. | O usuário `orchestrator` do container responde, mas tem `rolsuper=false` e `rolcreatedb=false`. | Teste foi mantido estrito; a execução integral requer um usuário de teste com permissão `CREATEDB`, conforme a infraestrutura prevista no projeto. |

## Verificação final

- `293` testes Python focados passaram, incluindo as validações do painel de progresso.
- Front: `15` testes Vitest e typecheck passaram.
- Worker Cloudflare: typecheck passou.
- Migration possui head único `20260804_0011`.
- A suíte Python completa percorreu os testes não PostgreSQL sem falhas de código; os
  casos PostgreSQL pararam no setup pela ausência de servidor em `127.0.0.1:5432`. A
  tentativa explícita em `55432` conectou, mas confirmou a falta de `CREATEDB` acima.

## Pendências ou bloqueios externos

- PostgreSQL de teste acessível com permissão para criar o banco temporário dos fixtures.
- Canário pago batch 1, explicitamente opt-in, após segredo público, quota e crédito.
