# MVP de Login via Cloudflare Access com Convites por E-mail e Claim Atômico

Data: `2026-08-31`

## Resultado

Implementado o ciclo completo de login e onboarding multiusuário baseado em Cloudflare Access e convites por e-mail:
- Deploy Worker e staging contract atualizados para manter tenant (`ORCH_ORGANIZATION_SLUG`, `ORCH_ORGANIZATION_NAME`) server-owned no worker e proibir injeção de headers `X-Orch-Organization-*` pelo cliente.
- Tabela `organization_invitations`, RLS restrito a owner/admin, e função atômica `public.claim_organization_invitation` (`SECURITY DEFINER` com validação de igualdade exata ao contexto da sessão `app.organization_id`/`app.user_id`).
- Onboarding via convites por e-mail pendente consumidos atomicamente na primeira requisição autenticada do usuário com claim `email` validada pelo JWT do Cloudflare Access.
- Comando administrativo idempotente `orchestrator db owner-bootstrap` com lock `FOR UPDATE` para inicializar a organização e convite de owner sem race conditions.
- Rotas de API `/api/v2/invitations` (`GET`, `POST`, `DELETE`) com validação de papéis canônicos RBAC e retorno de 409 em duplicidades concorrentes.
- Frontend com `SessionBoundary` envolvendo todas as rotas (incluindo wizard `/campaigns/new`), tratamento acessível de 401, 403 e 503, exclusão de chaves de autenticação da persistência do TanStack Query com buster `v2`, e tela de Settings com formulário email-only + role e tabela separada de convites pendentes.

## Mudanças de contrato

- Adicionada tabela PostgreSQL `organization_invitations` com constraints de chave primária composta `(organization_id, normalized_email)` e constraint de papel canônico.
- Adicionada função `public.claim_organization_invitation(p_organization_id, p_user_id, p_user_subject, p_email, p_display_name)` que consome o convite e materializa usuário e membership de modo atômico.
- Rotas de API `/api/v2/invitations` (`GET`, `POST`, `DELETE /api/v2/invitations/{email}`).
- Frontend contracts: adicionadas interfaces `Invitation` e `CreateInvitationBody` em `front/src/api/contracts.ts` e métodos correspondentes no cliente de API.

## RED → GREEN

- **RED:** `tests/test_staging_contract.py` falhou exigindo variáveis de organização no `sharedEnv` do Worker e ausência de headers `X-Orch-Organization`.
- **GREEN:** Atualizado `deploy/cloudflare/src/index.ts` com `ORCH_ORGANIZATION_SLUG` e `ORCH_ORGANIZATION_NAME` no `sharedEnv` e remoção dos headers client-side em `forwardApi`.
- **RED:** `tests/test_postgres_invitations.py` falhou na ausência da tabela `organization_invitations` e repositório de convites.
- **GREEN:** Criada migração `20260831_0013_organization_invitations.py`, modelo SQLAlchemy `OrganizationInvitation` e repositório `PostgresInvitationRepository` com `normalize_email`.
- **RED:** `tests/test_cloudflare_auth.py` e `tests/test_postgres_invitations.py` falharam no repasse de `verified_email` e claim atômico em `Database.authorize_tenant`.
- **GREEN:** Evoluído `Database.authorize_tenant(identity, verified_email=None)` e `CloudflareAccessMiddleware` para extrair claim `email` do JWT verificado e consumir convite automaticamente no primeiro request.
- **RED:** `test_owner_bootstrap_lifecycle_and_fail_closed` falhou em idempotência e fail-closed com organização preexistente.
- **GREEN:** Implementado `owner_bootstrap` com lock `FOR UPDATE` na organização e comando `orchestrator db owner-bootstrap` na CLI.
- **RED:** `tests/test_api_v2_auth.py` falhou na ausência dos endpoints `/api/v2/invitations`.
- **GREEN:** Criado `src/orchestrator/web/routes_invitations.py` e registrado no composition root `server.py`.
- **RED:** `front/src/api/queryClient.test.ts` e `front/src/components/SessionBoundary.test.tsx` falharam na persistência indevida de dados de auth no localStorage e ausência do boundary de sessão.
- **GREEN:** Criado `SessionBoundary.tsx`, excluídas chaves `session`, `members`, `invitations` em `shouldPersistQuery`, atualizado cache buster para `v2`, e atualizada tela de Settings com tabela de convites pendentes e formulário de convite email-only + role.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `tests/test_cloudflare_auth.py` lançou `TypeError: FakeDatabase.authorize_tenant() got an unexpected keyword argument 'verified_email'` | Mocks de teste com assinatura legada de 1 parâmetro. | Adicionada inspeção de assinatura em `_authorize_with_app_database` e no middleware para manter retrocompatibilidade com authorizers mock. |
| `test_claim_function_enforces_exact_session_context_equality` falhou com `current transaction is aborted` no segundo assert | Ambas as chamadas que provocam erro foram executadas no mesmo bloco de transação sem reset. | Separados os asserts em blocos de conexão independentes `async with db.connection(...)`. |
| `test_concurrent_owner_bootstrap_with_different_emails` falhou com `UniqueViolation` em `organizations_pkey` | Execução concorrente de `create_organization` tentou inserir o mesmo `id` gerado pelo slug simultaneamente. | Tratada a inserção concorrente em `create_organization` com `try/except` seguro no connect e serialização atômica via `SELECT FOR UPDATE` na linha da organização dentro de `owner_bootstrap`. |

## Verificação final

- `tests/test_staging_contract.py`: 8 passed (100% green).
- `tests/test_invitations.py`: 2 passed (100% green).
- `tests/test_roles_parity.py`: 3 passed (100% green).
- `tests/test_postgres_invitations.py`: 11 passed com PostgreSQL real e isolation database (100% green).
- `tests/test_cloudflare_auth.py` & `tests/test_auth_rbac.py` & `tests/test_api_v2_auth.py`: 50 passed (100% green).
- `npm --prefix front test`: 12 arquivos, 31 testes (100% green).
- `npm --prefix front run check:boundaries`: OK.
- `npm --prefix front run build`: OK (`tsc --noEmit && vite build`).
- `rtk proxy .venv/bin/ruff check src tests`: All checks passed (0 errors).

## Pendências ou bloqueios externos

Nenhum.
