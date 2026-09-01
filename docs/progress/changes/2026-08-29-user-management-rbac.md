# MVP multiusuário, RequestPrincipal, RBAC centralizado e gestão de membros

Data: `2026-08-29`

## Resultado

Implementado o MVP multiusuário para organização fixa sob Cloudflare Access OIDC e modo de desenvolvimento local desacoplado. A autorização foi centralizada em `RequestPrincipal` e `Permission`, aplicando matriz de papéis (`owner`, `admin`, `member`, `viewer`), proteção transacional do último owner, endpoints `/api/v2/me` e `/api/v2/members`, migração Alembic `20260829_0012` para metadados de perfil em `users` e interface React completa no frontend com sessão, role badge, logout e gerenciamento de membros.

## Mudanças de contrato

- **Sessão do Usuário:** `/api/v2/me` expõe id, subject, organização, papel (`role`), lista de permissões granulares e metadados públicos sanitizados (`email`, `display_name`).
- **Gestão de Membros:** Endpoints `/api/v2/members` com `GET` (listagem para admin/owner), `POST` (concessão de papel), `PATCH /api/v2/members/{subject}` (alteração de papel com bloqueio 409 para último owner) e `DELETE /api/v2/members/{subject}` (revogação com proteção do último owner).
- **Proteção RBAC em Rotas Mutantes:** Rotas `POST /api/v2/runs`, `POST /api/run`, `POST /api/run/{id}/retry`, `POST /api/v2/runs/{id}/review`, `POST /api/approve/*` e `POST/DELETE /api/prompts` agora exigem permissões explícitas (`runs:create`, `runs:review`, `runs:retry`, `runs:voice_reroll`, `prompts:write`), retornando 403 Forbidden para o papel `viewer`.
- **Database e RLS Seam:** `TenantContext` preserva `role` e `Database.authorize_tenant` extrai o papel real a partir de `organization_members`.
- **Frontend:** Atualização de `contracts.ts`, `queries.ts`, `Sidebar.tsx` (perfil do usuário, role badge, link de logout `/cdn-cgi/access/logout`, restrição visual do botão de nova campanha) e `Settings.tsx` (seção de gestão de membros com listagem, adição, troca de papel e revogação).

## RED → GREEN

- **RED:** Criação dos testes `tests/test_auth_rbac.py`, `tests/test_api_v2_auth.py`, `tests/test_members.py`, `front/src/api/queries.session.test.tsx` e `front/src/screens/Settings.test.tsx` demonstrando ausência de `Permission`, `RequestPrincipal`, rotas `/api/v2/me`, `/api/v2/members`, bloqueios 403 para viewer e controles de interface.
- **GREEN:** Implementação de `Permission`, `ROLE_PERMISSIONS`, `RequestPrincipal`, `get_current_principal` e `require_permission` em `src/orchestrator/auth.py`; atualização de `TenantContext` em `src/orchestrator/db/tenancy.py` e `Database.authorize_tenant` em `src/orchestrator/db/database.py`; criação de `src/orchestrator/db/members.py` (`PostgresMemberRepository` e `InMemoryMemberRepository`); criação de `src/orchestrator/web/routes_members.py`; aplicação de `require_permission` nas rotas web; migração Alembic `20260829_0012_user_management_rbac.py`; atualização de `Sidebar.tsx` e `Settings.tsx`.
- **REFACTOR:** Sincronização e isolamento de ContextVar entre requisições; tipagem estrita com Pydantic `extra="forbid"` em requests de membros; `open_repository` assíncrono para membros com fallback gracioso.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `ImportError: cannot import name 'RequestPrincipal'` ao importar `orchestrator.auth` em `db.members`. | Ciclo de importação de runtime entre `auth.py` e `db/members.py` via `db/__init__.py`. | Uso de `if TYPE_CHECKING:` para importar `RequestPrincipal` dentro de `db/members.py`. |
| `ValueError: contexto de tenant incompleto` em `get_current_principal`. | Modo `disabled` chamava `TenantIdentity.from_env()` estrito quando env vars não estavam setadas. | Implementado fallback gracioso para identidade local padrão (`local`, `Local Organization`, `local-user`) em modo não-Access. |
| `AttributeError: module 'orchestrator.web.server' has no attribute 'run_trace_config'`. | Import de `run_trace_config` foi omitido durante organização de imports em `server.py`. | Restaurado import `from orchestrator.tracing import run_trace_config` em `server.py`. |
| `TS6133: 'useMembersQuery' is declared but its value is never read` em `queries.session.test.tsx`. | Import do hook sem asserção correspondente no teste unitário de sessão. | Adicionada asserção de teste para validação de `useMembersQuery`. |

## Verificação final

- `rtk proxy .venv/bin/pytest tests/test_auth_rbac.py tests/test_cloudflare_auth.py tests/test_api_v2_auth.py tests/test_members.py --no-cov`: 36 testes passando (100%).
- `rtk proxy .venv/bin/pytest tests/test_api_v2.py tests/test_agent_catalog.py tests/test_web_endpoints.py tests/test_web_item_updates.py --no-cov`: 148 testes passando (100%).
- `rtk proxy .venv/bin/pytest --ignore-glob="*postgres*" --ignore-glob="*storage_migration*" --no-cov`: 1.476 testes passando (100%).
- `rtk pnpm --dir front test`: 9 arquivos de teste, 19 testes passando (100%).
- `rtk pnpm --dir front run build`: compilação Vite + TypeScript bem-sucedida.
- `rtk pnpm --dir front run check:boundaries`: fronteiras arquiteturais do frontend validadas (`Frontend boundaries OK`).

## Pendências ou bloqueios externos

Nenhum.
