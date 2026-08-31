# Progresso

Painel do estado atual. O histórico técnico completo fica em `docs/progress/archive/`;
novas entregas usam uma página própria em `docs/progress/changes/`.

## Estado atual

- A API/UI V2 expõe Configuração → Plano criativo → Revisão → Produção e QC → Montagem;
  distribuição continua fora do escopo e o estado terminal aprovado é `assembled`.
- Existe um único gate humano durável, `review_creative_plan`, com resolução versionada.
- `config-mock` é offline e determinístico; `config-staging` usa geração mock com
  infraestrutura real; `config` usa adapters live protegidos pelo kill switch de pagos.
- O perfil live integra ElevenLabs Voice Design, vídeo Replicate/PrunaAI, QC de ponteiros
  canônicos R2/S3, locução e montagem FFmpeg.
- O legado até 2026-08-03 está preservado integralmente e protegido por checksums no
  [manifesto do arquivo](progress/archive/MANIFEST.md).

## Trabalho pendente e bloqueios

- Executar um canário live novo com batch 1 somente após configurar webhook público,
  quota `replicate_video_seconds` e confirmar crédito; nunca retomar `web-7e526a1b`.
- Confirmar crédito nos providers pagos antes do mesmo aceite live.
- Acompanhar warnings upstream do LangGraph em cancelamentos deliberados; checkpoints e
  comportamento estão corretos, portanto não há mudança local segura pendente.

## Últimas 10 entregas

- [MVP de Login via Cloudflare Access com Convites por E-mail e Claim Atômico](progress/changes/2026-08-31-cloudflare-access-login-mvp.md) — tabela de convites, RLS restrito a owner/admin, claim atômico via SECURITY DEFINER com validação de contexto, owner-bootstrap idempotente, rotas /api/v2/invitations, SessionBoundary e exclusão de chaves sensíveis da persistência frontend.
- [Separação de papéis PostgreSQL e bootstrap no Docker Compose](progress/changes/2026-08-29-postgres-role-separation.md) — migrador `orchestrator` com `BYPASSRLS`, runtime `orchestrator_runtime` com `NOBYPASSRLS`, serviço one-shot `db-roles` no Compose, preflight check na migração 0012 e isolamento de tenant 100% verificado.
- [Revisão e endurecimento do MVP multiusuário e RBAC](progress/changes/2026-08-29-user-management-rbac-review-fixes.md) — tenant server-owned via Cloudflare Access, contrato de POST não-upsert com 409, políticas RLS reais para NOBYPASSRLS, proteção de concorrência com lock do último owner e frontend com auth_mode e permissões.
- [MVP multiusuário e RBAC centralizado](progress/changes/2026-08-29-user-management-rbac.md) — autenticação Cloudflare Access OIDC / dev local, RequestPrincipal centralizado, matriz viewer/member/admin/owner, proteção do último owner, rotas /api/v2/me e /api/v2/members, migração 20260829_0012 e gestão de membros no frontend React.
- [Injeção de repositório de artifacts e paridade no benchmark](progress/changes/2026-08-28-artifact-repository-and-benchmark-replay-fixes.md) — `run_pipeline` mantém repositório aberto e injeta no grafo sem criar SQLite indevido; live e replay no benchmark preservam drafts inválidos e `structural_reason` idênticos.
- [Seleção de modelo por campanha para roteiros (DeepSeek)](progress/changes/2026-08-26-per-campaign-script-model-selection.md) — suporte a `script_model` por campanha com validação server-side contra `allowed_models` no `agents.yaml` e whitelist do DeepSeek V4.
- [Benchmark isolado de modelos para scripts](progress/changes/2026-08-26-script-model-benchmark.md) — harness opt-in (`--live`) que compara deepseek/gemini/qwen com judge fixo claude-opus-5 via cassette, com custo estimado e razão score/custo.
- [Correções pós-revisão da refatoração estrutural](progress/changes/2026-08-25-refactor-review-fixes.md) — topologia runtime cobre tiers customizados de ponta a ponta, contratos de dict/gênero foram corrigidos e o storage R2 ficou seguro entre loops e cancelamentos.
- [Divisão do web/server.py em módulos](progress/changes/2026-08-25-server-split.md) — server.py de 2.253 para 625 linhas: events, run_executor, RunRegistry injetável e rotas por domínio, com gate dual-mode e payloads preservados.
- [Quebra de ciclos de camadas e vazamentos de adapters](progress/changes/2026-08-25-layering-adapters.md) — ArtifactRecord em módulo neutro, migrações sem grafo, accessors explícitos no CompositeAdapter e fallback mock injetado.

## Índice do histórico

- [Manifesto e inventário legado](progress/archive/MANIFEST.md) — origem, faixas,
  76 títulos e checksums dos arquivos imutáveis.
- [Junho de 2026](progress/archive/2026-06.md) — fundação, MVP, dashboard e falhas iniciais.
- [Julho de 2026](progress/archive/2026-07.md) — persistência durável, V2, infraestrutura,
  adapters live, QC e assembly.
- [Agosto de 2026](progress/archive/2026-08.md) — integração completa de Voice Design.
- [Mudanças novas](progress/changes/2026-08-03-reorganizacao-historico-progresso.md) —
  páginas detalhadas criadas após a migração.
- [Template obrigatório](progress/CHANGE-TEMPLATE.md) — estrutura para cada nova entrega.

## Como registrar uma mudança

1. Copie o [template](progress/CHANGE-TEMPLATE.md) para
   `docs/progress/changes/YYYY-MM-DD-slug.md` e preencha todas as seções.
2. Registre RED → GREEN, falhas como `Sintoma | Causa | Correção`, verificação e somente
   pendências acionáveis na página da mudança.
3. Adicione ao topo das últimas entregas uma linha com link e remova a mais antiga,
   mantendo exatamente dez itens e o painel abaixo de 250 linhas.
4. Guarde decisões arquiteturais em `docs/DECISIONS.md`/ADRs e procedimentos nos
   runbooks; aqui registre apenas resultado e link, sem duplicar comandos ou justificativas.
5. Nunca altere os arquivos mensais. Correções posteriores sempre ganham nova página.
