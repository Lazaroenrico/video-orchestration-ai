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

- [Gate final de regressão da refatoração LangChain](progress/changes/2026-08-18-gate-final-regressao-refatoracao.md) — validação completa das suítes offline (1.218 testes), LLM Judge cassette replay, matriz dos 3 perfis, review gate/checkpoint, efeitos/quotas/webhooks e lockfile determinístico.
- [Alinhar documentação canônica e instalação reproduzível](progress/changes/2026-08-18-alinhar-documentacao-canonica-e-instalacao.md) — alinhamento de D46/D47, ToolStrategy vs action tools, CompositeAdapter domain/media-only, diagramas V2 e instalação reproduzível frozen via uv.lock.
- [Remoção de bridge, aliases e dependências mortos](progress/changes/2026-08-18-remover-bridge-aliases-dependencias-mortos.md) — remoção de Topaz/Replicate upscale, Vercel video/assembly, script bridge Node, package.json raiz e Node do Dockerfile runtime.
- [Reduzir CreativeStageExecutor e catálogo aos três stages criativos](progress/changes/2026-08-18-reduzir-creativestageexecutor-e-catalogo.md) — redução do catálogo e executor aos stages `concepts`, `scripts` e `creator_profiles` com `materializer` único, remoção de metadados mortos e nós não criativos diretos.
- [Consolidação da implementação mock de linguagem no LanguageRuntime](progress/changes/2026-08-18-consolidar-mock-linguagem.md) — centralização da geração e schemas mock de linguagem em `LanguageRuntime`, purga de métodos de LLM do `MockAdapter` e bloqueio de fallbacks.
- [Separar submissão LangChain da materialização de criativos](progress/changes/2026-08-18-separar-submissao-langchain-materializacao.md) — desacoplamento do `LanguageRuntime.generate_structured` retornando Pydantic sem callbacks e materialização server-owned no executor.
- [Correção de CI (FFmpeg, migração 0007 e guards operacionais)](progress/changes/2026-08-18-ci-fixes-ffmpeg-migration.md) — instalação de FFmpeg no host de teste, inserção retrocompatível no teste de migração 0007 e guards de secrets em staging.
- [Migração do adapter de imagem para AsyncOpenAI](progress/changes/2026-08-17-migrar-adapter-imagem-asyncopenai.md) — transição para AsyncOpenAI com client injetável, compatibilidade Vercel Gateway e retries determinísticos.
- [Centralização de deployments LangChain em LanguageModelFactory](progress/changes/2026-08-17-centralizar-deployments-langchain-factory.md) — factory centralizada com `init_chat_model`, paridade de credenciais/URLs/retries e delegação em `LanguageRuntime`.
- [Classificação de aliases, adapters e integrações legadas](progress/changes/2026-08-17-classificacao-aliases-e-integracoes.md) — inventário de 19 itens classificados em `supported`, `compatibility` e `dead` com plano de rollback para subsidiar a issue #16.

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
