# Separação de papéis PostgreSQL e bootstrap no Docker Compose

Data: `2026-08-29`

## Resultado

Consolidação da separação estrita de papéis PostgreSQL no ambiente local e no Docker Compose. O papel `orchestrator` atua exclusivamente como migrador e proprietário local com `BYPASSRLS`, enquanto a API e os Runners conectam-se unicamente via `orchestrator_runtime` (`NOBYPASSRLS`). O bootstrap de papéis e permissões no Docker Compose foi desacoplado em um serviço one-shot idempotente (`db-roles`), preservando dados em volumes existentes e estabelecendo a cadeia determinística `postgres (healthy) -> db-roles (completed) -> migrate (completed) -> api`. As funções `SECURITY DEFINER` foram endurecidas com `SET search_path = pg_catalog` (sem schemas graváveis por runtime) e os fixtures de teste foram isolados para não vazar mutações de senha no cluster dev.

## Mudanças de contrato

- **Papéis PostgreSQL Locais:**
  - `orchestrator`: `LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`. Papel exclusivo de `MIGRATION_DATABASE_URL` para aplicar migrações e comandos administrativos `db org-create`/`membership-grant`.
  - `orchestrator_runtime`: `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION`. Papel exclusivo de `DATABASE_URL` para o runtime da API e Runners.
  - `MIGRATION_DATABASE_URL` e `DATABASE_URL` no Docker Compose e ambientes locais não são mais iguais nem compartilham privilégios.
- **Docker Compose:**
  - Adicionado serviço one-shot `db-roles` que executa `infra/postgres/10-app-role.sql` autenticado como superuser `postgres`.
  - Cadeia de dependências com `condition: service_completed_successfully` em `migrate` (depende de `db-roles`) e em `api` (depende de `migrate`).
- **Migração `20260829_0012`:**
  - Preflight check inicial valida se a conexão migradora possui privilégio `BYPASSRLS` ou `SUPERUSER`, falhando rápido com `RuntimeError` explicativo se executada incorretamente sob papel de runtime.
  - Funções `current_actor_role()` e `organization_has_members()` com `SECURITY DEFINER`, `SET search_path = pg_catalog` (sem `public` ou `pg_temp`), `SET row_security = off`, `REVOKE EXECUTE FROM PUBLIC` e concessão explícita para papéis runtime.
  - Políticas de RLS em `users` ajustadas (`users_select_rbac`, `users_insert_rbac`, `users_update_rbac`) garantindo isolamento estrito cross-tenant e suporte a concessões de membership.
- **Mecanismos Administrativos e Staging:**
  - `provision_runtime_role` em `src/orchestrator/db/admin.py` concede privilégios de execução de funções existentes e futuras (`ALTER DEFAULT PRIVILEGES ... GRANT EXECUTE ON FUNCTIONS`).
  - `docs/STAGING.md` documenta a separação obrigatória entre `MIGRATION_DATABASE_URL` e `DATABASE_URL`, a tabela de atributos esperados por papel e a ordem determinística de rollout.
  - `tests/conftest.py` preserva o contrato padrão do projeto sem default implícito para a porta 55432, aceitando `PGHOST`/`PGPORT` explícitos quando configurados.

## RED → GREEN

- **RED:**
  - Criação de `tests/test_compose_config.py` e `tests/test_postgres_role_bootstrap.py` comprovando que o Compose dependia de inicialização do volume e que migrações ou runtime sob `NOBYPASSRLS` falhavam ou burlavam isolamento.
  - Fortalecimento de `tests/test_postgres_role_bootstrap.py` exigindo `search_path=pg_catalog` exato em `current_actor_role` e `organization_has_members`, falhando em assertions quando `public, pg_temp` estava presente.
  - Demonstração de que `test_provision_runtime_role_grants_function_permissions` alterava a senha de `orchestrator_runtime` no cluster compartilhado sem restauração subsequente.
- **GREEN:**
  - Atualização de `infra/postgres/10-app-role.sql` com blocos `DO $$ ... $$` idempotentes para `orchestrator` (`BYPASSRLS`) e `orchestrator_runtime` (`NOBYPASSRLS`).
  - Configuração do serviço `db-roles` e separação das variáveis `DATABASE_URL` e `MIGRATION_DATABASE_URL` no `docker-compose.yml`.
  - Implementação de preflight check, `SET search_path = pg_catalog` e hardening de ACLs na migração `20260829_0012`.
  - Atualização de `provision_runtime_role` com `GRANT EXECUTE ON ALL FUNCTIONS` e `ALTER DEFAULT PRIVILEGES`.
  - Inserção limpa de usuários no `PostgresMemberRepository.grant_member` compatível com RLS.
  - Bloco `finally` em `test_provision_runtime_role_grants_function_permissions` restaurando a senha padrão, seguido de uma conexão real que comprova a autenticação com a credencial restaurada.
- **REFACTOR:** Padronização dos fixtures e helpers de teste em `test_postgres_foundation.py` e `test_postgres_members.py` para suportar PostgreSQL 16 com `ADMIN OPTION` e isolamento cross-database.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `psycopg.errors.InsufficientPrivilege: query would be affected by row-level security policy for table "organization_members"` | Execução de migração com `SET row_security = off` em funções `SECURITY DEFINER` por papel sem `BYPASSRLS`. | Adicionado preflight check na migração 0012 exigindo `BYPASSRLS` e atribuído `BYPASSRLS` exclusivamente ao migrador `orchestrator`. |
| `psycopg.errors.InsufficientPrivilege: new row violates row-level security policy for table "users"` | `INSERT ... ON CONFLICT DO UPDATE` em `grant_member` avaliava `users_select_rbac` antes do registro de membership existir. | Utilizado insert direto com tratamento de unicidade e políticas RLS de `users` ajustadas para o ciclo de vida do tenant. |
| `psycopg.errors.DependentObjectsStillExist: role "orchestrator_runtime" cannot be dropped` | Testes executavam `DROP ROLE` em banco de testes enquanto o papel possuía permissões no banco `orchestrator`. | Substituído `DROP ROLE` por `ALTER ROLE ... NOSUPERUSER NOBYPASSRLS` idempotente. |
| `psycopg.errors.InsufficientPrivilege: permission denied to alter role` | PostgreSQL 16 exige `WITH ADMIN OPTION` para que papéis não-superuser com `CREATEROLE` alterem outros papéis. | Concedido `GRANT orchestrator_runtime TO managed_migration_admin WITH ADMIN OPTION` nos cenários de teste simulados. |
| `AssertionError: current_actor_role deve ter search_path=pg_catalog exato` | Funções continham `search_path=public, pg_temp`, permitindo potencialmente schemas graváveis no search path. | Atualizado para `SET search_path = pg_catalog` na migração 0012 com todos os objetos qualificados. |
| `password authentication failed for user "orchestrator_runtime"` no smoke pós-testes | `test_provision_runtime_role_grants_function_permissions` alterava a senha global para `new_runtime_password` e `tests/conftest.py` apontava por padrão para 55432. | Restaurado `postgresql_noproc` padrão sem default 55432 e adicionada restauração da senha em `finally` no teste de provisionamento. |

## Verificação final

- `tests/test_compose_config.py`, `tests/test_postgres_role_bootstrap.py`, `tests/test_postgres_members.py`, `tests/test_postgres_foundation.py`: 38 testes executados e aprovados (100% GREEN contra PostgreSQL real com PGPORT=55432 explícito).
- `tests/test_cloudflare_auth.py`, `tests/test_auth_rbac.py`, `tests/test_api_v2_auth.py`, `tests/test_members.py`: 40 testes unitários e de integração de rotas e RBAC aprovados (100% GREEN).
- Frontend vitest: 10 suítes, 23 testes executados e aprovados (100% GREEN).
- `ruff check src tests`: todos os arquivos em conformidade (zero erros).
- `git diff --check`: zero conflitos ou problemas de whitespace.

## Pendências ou bloqueios externos

Nenhum.
