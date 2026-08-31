# Staging Cloudflare + Neon

O staging mantém todos os adapters de geração em `mock`; só PostgreSQL, R2,
Cloudflare Access, Queue e Containers são reais. Assim, o ambiente exercita o motor
distribuído sem chamar provider pago.

## Banco e identidade

O projeto Neon fica em `aws-sa-east-1`, PostgreSQL 16, com sete dias de PITR. Os dois
segredos de conexão precisam apontar ao endpoint **direto** e nunca podem ser iguais:

- `MIGRATION_DATABASE_URL`: papel administrativo/migrador privilegiado (`BYPASSRLS`),
  usado exclusivamente pelo job de migração e pelos comandos administrativos (`db org-create`,
  `membership-grant`). Em staging, utiliza o papel com `BYPASSRLS` provisionado pelo Neon;
  localmente, utiliza o papel `orchestrator` (`LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`).
- `DATABASE_URL`: papel não-proprietário de runtime `orchestrator_runtime`, estritamente
  configurado com `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`,
  usado exclusivamente por API e Runner.

### Atributos esperados dos papéis

| Papel | Escopo de Uso | Atributos Obrigatórios | URL de Conexão |
| --- | --- | --- | --- |
| Migrador (`orchestrator`) | Migrações Alembic e CLI admin | `LOGIN NOSUPERUSER BYPASSRLS` | `MIGRATION_DATABASE_URL` |
| Runtime (`orchestrator_runtime`) | API FastAPI e Runners | `LOGIN NOSUPERUSER NOBYPASSRLS` | `DATABASE_URL` |

### Ordem de provisionamento e preflight BYPASSRLS

A migração `20260829_0012` possui um preflight check que valida se a conexão migradora
possui `BYPASSRLS` ou `SUPERUSER`, falhando rápido com `RuntimeError` caso executada
incorretamente com o papel de runtime. A ordem obrigatória de rollout é:

1. **Bootstrap de Papéis:**
   - **Local / Compose:** o serviço one-shot `db-roles` executa `infra/postgres/10-app-role.sql`
     autenticado como `postgres` assim que o serviço de banco estiver saudável (`postgres: condition: service_healthy`).
   - **Staging / Neon:** o OpenTofu provisiona o banco e o secret `runtime_role_password`.
2. **Execução de Migrações:**
   - O job `migrate` executa `orchestrator migrate` conectado via `MIGRATION_DATABASE_URL`.
3. **Hardening de Runtime e Grants:**
   - O workflow executa `orchestrator db provision-runtime` via `MIGRATION_DATABASE_URL`
     para conceder privilégios em tabelas, sequences e funções (`EXECUTE ON ALL FUNCTIONS` e
     `ALTER DEFAULT PRIVILEGES`) ao `orchestrator_runtime`.
4. **Inicialização da Aplicação:**
   - API e Runners iniciam conectados unicamente via `DATABASE_URL` (`orchestrator_runtime`).

Não use hostname `-pooler`/pooled em nenhuma dessas URLs. O checkpointer do LangGraph
depende de escopo transacional e de sessão previsível; migrações também exigem conexão
direta.

Access valida a identidade do usuário através do JWT verificado (`sub` canônico e claim `email`). Depois do primeiro deploy/apply de migrações:

```bash
orchestrator db owner-bootstrap \
  --slug staging \
  --name "UGC Orchestrator Staging" \
  --email "founder@example.com"
```

O comando `owner-bootstrap` cria a organização e um convite pendente para o primeiro `owner` de forma idempotente. Quando o owner faz seu primeiro login via Cloudflare Access, o convite é consumido atomicamente na transação da requisição (`claim_organization_invitation`), materializando o usuário e sua membership `owner`.

Novos membros são convidados pela API autenticada (`POST /api/v2/invitations`) por administradores e proprietários. O onboarding é concluído no primeiro request em que o JWT verificado do Cloudflare Access apresentar o e-mail convidado.

Para contas de serviço e acesso direto de emergência (break-glass), utilize `membership-grant`:

```bash
orchestrator db membership-grant \
  --organization-slug staging \
  --user-subject 'service|cloudflare-runner' \
  --role member
```

Todos os comandos de CLI administrativa usam `MIGRATION_DATABASE_URL`. Revogue memberships ativas com `db membership-revoke`.

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
