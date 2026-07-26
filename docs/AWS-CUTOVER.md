# Exercício e cutover Cloudflare → AWS

O OpenTofu em `infra/aws-staging` cria ECR imutável, ECS/Fargate API e Runner, ALB, SQS
com DLQ, S3 privado/versionado, IAM mínimo, logs e alarmes. `api_desired_count=0` e
`runner_desired_count=0` são defaults deliberados: o primeiro apply prova o plano de
controle sem iniciar tráfego ou consumir jobs.

## Pré-condições

- O mesmo SHA OCI do deploy Cloudflare existe no ECR e a tag é imutável.
- As tasks usam Linux/amd64, `awsvpc`, conexão PostgreSQL direta e os mesmos schemas.
- O tenant/service user AWS já tem membership; JWT continua identificando e PostgreSQL
  continua autorizando.
- `S3_BUCKET`, R2, SQS e secrets foram validados por um task smoke com adapters mock.
- Backup/restore e `orchestrator ops inspect-run RUN_ID` estão verdes.

## Exercício sem tráfego

Dispare `Exercise AWS portability` com o SHA e `apply_no_traffic=true`. O workflow executa
`tofu plan`, exige a decisão explícita e aplica os dois services com contagem zero.
Depois rode manualmente uma API task e uma Runner task de smoke; não altere DNS.

## Migração dos objetos

Mantenha leitura dupla com:

```text
STORAGE_BACKEND=dual
STORAGE_WRITE_BACKEND=s3
```

Para cada run, execute `orchestrator storage migrate-run RUN_ID`. A ferramenta lê a key
exata no R2, verifica o SHA-256 canônico, grava a mesma key/content-type/metadata no S3,
confere `HeadObject` e só então troca `artifacts.storage_backend` para `s3`. Repetir o
comando é idempotente. Divergência mantém o ponteiro R2 e exige investigação.

## Cutover

1. Na borda, pausar novos jobs (`POST /api/run`), mantendo consultas e SSE.
2. Drenar jobs running na Cloudflare; aguardar leases ou recuperar explicitamente os
   vencidos. Confirmar outbox pendente zero e gates persistidos.
3. Copiar runs R2→S3 e manter `STORAGE_BACKEND=dual`; validar URLs R2 e S3.
4. Trocar a outbox para `ORCH_QUEUE_BACKEND=sqs`, iniciar um Runner ECS e confirmar
   claim/heartbeat/eventos. SQS é só wake-up; o PostgreSQL continua canônico.
5. Iniciar a API ECS, apontar somente tráfego interno/canário do Worker Cloudflare ao ALB
   e confirmar `/readyz`, autenticação, tenant e replay por `Last-Event-ID`.
6. Rodar um batch mock 2, abrir/resolver gate, derrubar Runner e confirmar recuperação.
7. Fazer a **decisão Go/No-Go**. Em Go, mudar a origem gradualmente; em No-Go, devolver
   publisher/origem à Cloudflare sem desfazer dados já copiados.
8. Manter leitura dupla até inventário sem divergências e janela de rollback encerrada;
   somente então usar `STORAGE_BACKEND=s3`.

## Invariantes de rollback

Runs antigos seguem consultáveis pelo mesmo `run_id`; eventos e checkpoints não mudam.
Objetos ainda marcados `r2` continuam assinados no R2 e os marcados `s3` no S3. Nunca
edite `storage_key`, nunca persista signed URL e nunca redirecione a Queue antes de
drenar/reconciliar leases e outbox.
