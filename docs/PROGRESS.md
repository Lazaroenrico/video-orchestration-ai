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

- [Injeção de repositório de artifacts e paridade no benchmark](progress/changes/2026-08-28-artifact-repository-and-benchmark-replay-fixes.md) — `run_pipeline` mantém repositório aberto e injeta no grafo sem criar SQLite indevido; live e replay no benchmark preservam drafts inválidos e `structural_reason` idênticos.
- [Seleção de modelo por campanha para roteiros (DeepSeek)](progress/changes/2026-08-26-per-campaign-script-model-selection.md) — suporte a `script_model` por campanha com validação server-side contra `allowed_models` no `agents.yaml` e whitelist do DeepSeek V4.
- [Benchmark isolado de modelos para scripts](progress/changes/2026-08-26-script-model-benchmark.md) — harness opt-in (`--live`) que compara deepseek/gemini/qwen com judge fixo claude-opus-5 via cassette, com custo estimado e razão score/custo.
- [Correções pós-revisão da refatoração estrutural](progress/changes/2026-08-25-refactor-review-fixes.md) — topologia runtime cobre tiers customizados de ponta a ponta, contratos de dict/gênero foram corrigidos e o storage R2 ficou seguro entre loops e cancelamentos.
- [Divisão do web/server.py em módulos](progress/changes/2026-08-25-server-split.md) — server.py de 2.253 para 625 linhas: events, run_executor, RunRegistry injetável e rotas por domínio, com gate dual-mode e payloads preservados.
- [Quebra de ciclos de camadas e vazamentos de adapters](progress/changes/2026-08-25-layering-adapters.md) — ArtifactRecord em módulo neutro, migrações sem grafo, accessors explícitos no CompositeAdapter e fallback mock injetado.
- [Runtime mode centralizado](progress/changes/2026-08-25-runtime-mode.md) — runtime_mode.py substitui leituras espalhadas de DATABASE_URL/ORCH_ENABLE_PAID_ADAPTERS e throttle ganha reset para testes.
- [Topologia única dos nodes do grafo](progress/changes/2026-08-25-node-topology.md) — graph/topology.py passa a ser a fonte canônica; progress.py e web derivam suas visões dela, com validador anti-divergência.
- [Config base + overlay](progress/changes/2026-08-25-config-overlay.md) — config-base/ compartilhado elimina a triplicação (18 cópias de prompts); perfis viram overlays mínimos com prova de equivalência byte-a-byte.
- [Primitivas comuns](progress/changes/2026-08-25-common-primitives.md) — orchestrator/common/ unifica to_plain, wav_data_uri, status terminais e inferência de gênero que estavam duplicados em até 4 pontos.

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
