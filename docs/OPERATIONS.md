# Operação e recuperação do staging

Este runbook cobre o staging Cloudflare/Neon em modo mock. Os objetivos congelados da
ADR-D36 são **RPO <= 5 min** e **RTO <= 60 min**. O histórico/PITR do Neon, configurado
para sete dias, é a proteção dentro do RPO; o dump lógico diário é uma segunda cópia
portável e não substitui PITR.

## Reconstruir um run

Use a URL direta do papel runtime e o tenant correto:

```bash
orchestrator ops inspect-run RUN_ID
```

O JSON reúne `runs`, `run_items`, jobs/tentativas/leases, gates e resoluções, eventos em
sequência, artifacts e efeitos externos. Nenhum dado vem de `_runs`, buffer SSE ou disco
do Container. Os traces LangSmith complementam o relatório quando
`LANGSMITH_TRACING=true`; logs operacionais usam `ORCHESTRATOR_LOG_FORMAT=json` e os
campos `run_id`, `job_id` e `organization_id`.

## Alertas

O job `Operate staging` executa `orchestrator ops maintain` diariamente e entrega um
snapshot JSON. Encaminhe qualquer alerta para o canal de plantão:

- `expired_job_lease`: Runner morreu ou perdeu conectividade; confirme heartbeat e deixe
  o próximo claim recuperar o job.
- `outbox_dlq`: publicação falhou cinco vezes; corrija Queue/credencial e reencaminhe a
  entrada com auditoria.
- `storage_signing_error`: valide endpoint, clock, key e credenciais R2.
- `stream_lag`: run ativo sem evento recente; correlacione job/trace e o estado do
  provider.
- `provider_limit`: consumo atingiu o limiar da quota global; não aumente concorrência
  antes de confirmar crédito e rate limit.
- `anomalous_spend`: custo persistido do run excedeu o limiar; pause adapters pagos e
  reconcilie `external_effects`.

## Backup e restore

O workflow diário usa `pg_dump --format=custom` na conexão direta
`STAGING_MIGRATION_DATABASE_URL`, calcula SHA-256, restaura o dump em PostgreSQL 16 vazio
e só então arquiva dump e checksum no bucket R2 privado. Falha em qualquer etapa invalida
o backup.

Restore de desastre:

1. Pause novos `POST /api/run` na borda, mas mantenha consultas.
2. Escolha PITR do Neon anterior ao incidente (preferencial, RPO de cinco minutos) ou o
   último dump com checksum válido.
3. Restaure em branch/banco novo, rode `orchestrator migrate` e `ops inspect-run` em uma
   amostra de runs/gates.
4. Troque `DATABASE_URL` e `MIGRATION_DATABASE_URL`, reinicie API/Runner e acompanhe
   leases expiradas, replay SSE e DLQ.
5. Registre tempos: o serviço deve voltar em até 60 minutos. Não apague o banco anterior
   até concluir a reconciliação.

## Objetos e retenção

O inventário nasce dos ponteiros canônicos do PostgreSQL e executa `HeadObject`/`exists`
no backend; ele não usa listagem cega do bucket como fonte de verdade. `missing` não é
apagado automaticamente. O purge remove primeiro os bytes expirados e só depois a linha
do artifact, preservando retry seguro.

## Replicate live durável

Antes de habilitar vídeo pago, configure `ORCH_PUBLIC_API_BASE_URL` com uma origem HTTPS
alcançável pelo Replicate, `REPLICATE_WEBHOOK_SIGNING_SECRET` com o segredo `whsec_...`
do provider e `ORCH_WEBHOOK_CORRELATION_SECRET` com um segredo próprio forte. Na borda
Cloudflare, `/webhooks/replicate/*` deve ficar fora da política Access: essa rota usa a
assinatura Replicate e o token de correlação, enquanto `/api/*` continua protegido por
Access. `/readyz` recusa o perfil live durável/pago quando algum desses campos falta.

Reserve o teto global em segundos antes do kill switch:

```bash
orchestrator db set-provider-quota \
  --provider replicate_video_seconds \
  --limit-units TOTAL_DE_SEGUNDOS
```

No desenvolvimento Compose live, o equivalente é
`./scripts/dev-local video-quota --seconds TOTAL_DE_SEGUNDOS`; a URL pública ainda precisa
ser um túnel HTTPS, nunca `localhost`. Só depois habilite `ORCH_ENABLE_PAID_ADAPTERS=true`.

Para uma prediction ambígua, consulte `orchestrator ops inspect-run RUN_ID` e correlacione
`external_effects.effect_key`, `provider_operation_id`, `provider_status` e `error_type`.
Não emita outro POST e não altere quota/ledger manualmente. Polling e webhook convergem no
mesmo registro. Callback recebido depois do timeout registra atividade, mas não reabre a
campanha; se for necessária nova execução, use retry manual, que cria outro `run_id`.

O canário pago é sempre opt-in: campanha nova, batch 1, quota mínima e crédito confirmado.
Testes automatizados nunca fazem essa chamada.

## Exercícios trimestrais

Rode carga mock batch 2, derrube o Runner após o claim, confirme recuperação do lease,
reconecte SSE por `Last-Event-ID`, restaure o último backup e repita o teste de isolamento
Acme/Globex. Anote p95 da API, retomada do gate, RPO real, RTO real e divergências do
inventário.
