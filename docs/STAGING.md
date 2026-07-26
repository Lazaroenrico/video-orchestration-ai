# Staging Cloudflare + Neon

O staging mantém todos os adapters de geração em `mock`; só PostgreSQL, R2,
Cloudflare Access, Queue e Containers são reais. Assim, o ambiente exercita o motor
distribuído sem chamar provider pago.

## Banco e identidade

O projeto Neon fica em `aws-sa-east-1`, PostgreSQL 16, com sete dias de PITR. Os dois
segredos de conexão precisam apontar ao endpoint **direto**:

- `MIGRATION_DATABASE_URL`: papel privilegiado usado exclusivamente pelo job de
  migração e pelos comandos administrativos.
- `DATABASE_URL`: papel fixo `orchestrator_runtime`, sem `SUPERUSER`/`BYPASSRLS`,
  usado por API e Runner.

Não use hostname `-pooler`/pooled em nenhuma dessas URLs. O checkpointer do LangGraph
depende de escopo transacional e de sessão previsível; migrações também exigem conexão
direta. O workflow executa `orchestrator migrate` antes do rollout e depois endurece o
papel runtime com `orchestrator db provision-runtime`.

Access valida a identidade, mas não concede acesso. Depois do primeiro apply:

```bash
orchestrator db org-create \
  --slug staging \
  --name "UGC Orchestrator Staging"

orchestrator db membership-grant \
  --organization-slug staging \
  --user-subject 'service|cloudflare-runner' \
  --role member

orchestrator db membership-grant \
  --organization-slug staging \
  --user-subject '<sub do JWT Access>' \
  --role member
```

Todos usam `MIGRATION_DATABASE_URL`. Revogue com `db membership-revoke`; a API nunca
cria organização, usuário ou membership durante uma requisição.

## Provisionamento

1. Copie `infra/staging/terraform.tfvars.example` para um arquivo fora do git e
   exporte `CLOUDFLARE_API_TOKEN`, `NEON_TOKEN` e
   `TF_VAR_runtime_role_password`.
2. Rode `tofu init`, `tofu plan -out=staging.tfplan` e só então
   `tofu apply staging.tfplan`.
3. Grave os outputs sensíveis no cofre do CI. Configure no Worker os secrets
   `DATABASE_URL`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `CF_ACCESS_TEAM_DOMAIN`,
   `CF_ACCESS_AUDIENCE` e `ORCH_INTERNAL_TOKEN`.
4. Ajuste o hostname placeholder do `wrangler.jsonc` ao domínio provisionado antes
   do primeiro deploy.

O bucket R2 é privado. O browser recebe apenas URLs assinadas de vida curta; CORS
permite somente `GET`/`HEAD` a partir do origin de staging.

## Execução e recuperação

O Worker serve o SPA, encaminha `/api/*` e SSE sem buffering, injeta o tenant de
staging e preserva `Cf-Access-Jwt-Assertion`. Um `POST /api/run` publica apenas um
wake-up na Queue. O consumidor chama o Runner HTTP, e o Runner reivindica no máximo
um job no PostgreSQL. O callback da Queue nunca executa a pipeline.

Um cron por minuto drena outbox, jobs pendentes e leases recuperáveis. Atualizações de
Containers usam imagem tagueada pelo SHA, grace period e rollout gradual. Runs ativos
permanecem no PostgreSQL/checkpoint e podem ser retomados por outra instância.
