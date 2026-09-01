# Revisão e endurecimento do MVP multiusuário e RBAC

Data: `2026-08-29`

## Resultado

Implementado o endurecimento arquitetural e de segurança do MVP multiusuário conforme especificação supervisionada:
1. Derivação de contexto de organização estritamente server-owned no `CloudflareAccessMiddleware` (via `ORCH_ORGANIZATION_SLUG` e `ORCH_ORGANIZATION_NAME`), rejeitando e ignorando cabeçalhos de request forjados e falhando de forma segura com HTTP 503 se a configuração do servidor estiver ausente.
2. Contrato estrito de `POST /api/v2/members` não-upsert: retorna HTTP 409 Conflict (`ExistingMemberError`) caso o membro já exista na organização, exigindo `PATCH /api/v2/members/{subject}` para alteração de papéis.
3. Resolução da recursão de RLS em `organization_members`: criação de funções SQL auxiliares `public.current_actor_role()` e `public.organization_has_members()` com `SECURITY DEFINER`, `search_path = public, pg_temp` e `row_security = off`, garantindo políticas não-recursivas sob o role `NOBYPASSRLS` na migração `20260829_0012_user_management_rbac.py`.
4. Proteção contra race condition no rebaixamento/remoção concorrente do último owner através do bloqueio da linha da organização (`SELECT id FROM organizations WHERE id = ... FOR UPDATE` no Postgres e `asyncio.Lock` no repositório in-memory).
5. Frontend React atualizado com campo `auth_mode` no contrato de sessão, exibição do link `/cdn-cgi/access/logout` restrita ao modo `cloudflare_access`, estados explícitos de `loading`, `empty` e `error` no gerenciador de membros, componente visual compartilhado `RoleBadge` e controle de visibilidade/bloqueio de ações mutáveis por permissão.
6. Bootstrap explícito e validação de `NOSUPERUSER NOBYPASSRLS` nos testes de integração PostgreSQL em `tests/test_postgres_members.py`.

## Mudanças de contrato

- `CloudflareAccessMiddleware`: Não aceita mais `X-Orch-Organization-Slug` ou `X-Orch-Organization-Name` vindos do cliente; o tenant é derivado exclusivamente da configuração do servidor (`ORCH_ORGANIZATION_SLUG`, `ORCH_ORGANIZATION_NAME`), retornando HTTP 503 com detalhe explicativo se a variável estiver ausente.
- `POST /api/v2/members`: Não realiza upsert de papel. Se o usuário já possuir membership no tenant, retorna HTTP 409 Conflict.
- `GET /api/v2/me`: Contrato inclui `auth_mode: "cloudflare_access" | "disabled"`.
- Frontend: Link de logout `/cdn-cgi/access/logout` é renderizado apenas quando `auth_mode === "cloudflare_access"`. Ações de criação de campanha, revisão, retry, reroll de voz e escrita de prompts são bloqueadas/ocultadas quando o usuário não possui a permissão correspondente.

## RED → GREEN

- **RED:** `tests/test_cloudflare_auth.py` falhou ao injetar headers de cliente forjados e não encontrar a configuração de servidor.
- **GREEN:** `src/orchestrator/auth.py` atualizado para ler `os.environ` da organização e rejeitar headers de cliente, com resposta segura 503 na ausência de configuração.
- **RED:** `tests/test_members.py` falhou ao esperar HTTP 409 em `POST /api/v2/members` com membro repetido (anteriormente executava upsert silencioso).
- **GREEN:** Criação de `ExistingMemberError` em `src/orchestrator/db/members.py` e mapeamento para HTTP 409 no endpoint FastAPI `src/orchestrator/web/routes_members.py`.
- **RED:** Subquery direta em `organization_members` dentro da policy de `organization_members` causava risco de `infinite recursion detected in policy` no PostgreSQL.
- **GREEN:** Criação de funções `public.current_actor_role()` e `public.organization_has_members()` como `SECURITY DEFINER` com `row_security = off` na migração `20260829_0012`.
- **RED:** Testes concorrentes de demote simultâneo do último owner demonstraram risco de corrida em contagem de owners sem lock serializado.
- **GREEN:** Implementado `SELECT id FROM organizations WHERE id = ... FOR UPDATE` nas mutações de membership no PostgreSQL e `asyncio.Lock` no repositório em memória.
- **RED:** `front/src/layout/Sidebar.test.tsx` e `front/src/screens/Settings.test.tsx` falharam na verificação de `auth_mode` e estados vazios/erro de membros.
- **GREEN:** Criação de `RoleBadge.tsx`, tratamento de `loading`, `empty` e `error` em `Settings.tsx` e adequação dos testes de frontend com Vitest.
- **REFACTOR:** Ruff linting executado em todo o backend (`src/` e `tests/`) com 100% de conformidade.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Cabeçalhos `X-Orch-Organization-Slug` podiam ser injetados por clientes | `CloudflareAccessMiddleware` lia headers HTTP antes de consultar o ambiente | Removida a leitura de headers de requisição para identificação de tenant; leitura restrita a `os.environ`. |
| Concessão de membro via POST alterava papel de membro pré-existente | `grant_member` usava `ON CONFLICT DO UPDATE` sem distinguir inserção de alteração | Implementado check explícito de existência levantando `ExistingMemberError` com retorno HTTP 409. |
| Recursão infinita em policies de `organization_members` no PostgreSQL | A policy continha subquery que consultava a própria tabela `organization_members` protegida por RLS | Criada função `SECURITY DEFINER` (`current_actor_role`) com `row_security = off` e `search_path` fixo para lookup não-recursivo. |
| Possibilidade de corrida em rebaixamento simultâneo de múltiplos owners | Contagem de owners lia estado sem bloqueio compartilhado da organização | Adicionado bloqueio `FOR UPDATE` na tabela `organizations` durante mutações de membros. |

## Verificação final

- `rtk proxy .venv/bin/pytest tests/test_cloudflare_auth.py tests/test_auth_rbac.py tests/test_api_v2_auth.py tests/test_members.py --no-cov`: 40 testes passando (100% green).
- `rtk pnpm --dir front test -- --run`: 10 arquivos de teste e 23 testes passando (100% green).
- `rtk pnpm --dir front run build && rtk pnpm --dir front run check:boundaries`: Build TypeScript/Vite concluído sem erros e boundaries validadas.
- `rtk proxy .venv/bin/ruff check src tests`: 0 violações de lint.
- `rtk git diff --check`: 0 erros de sintaxe ou whitespace.
- Testes PostgreSQL em `tests/test_postgres_members.py`: Estruturados com bootstrap administrativo explícito, verificação de role `NOSUPERUSER NOBYPASSRLS`, isolamento cross-tenant e concorrência; execução local não realizada por ausência de servidor PostgreSQL em `127.0.0.1:5432` no sandbox (limitação factual de infraestrutura local, sem uso de skip/xfail).

## Pendências ou bloqueios externos

- Validação em pipeline CI/CD ou ambiente de homologação com servidor PostgreSQL ativo para execução dos testes de integração de `tests/test_postgres_members.py`.
