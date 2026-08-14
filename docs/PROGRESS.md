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

- [Runtime de linguagem nativo LangChain](progress/changes/2026-08-14-langchain-native-runtime.md) — composição central de dependências, models/agents nativos, mock determinístico e vídeo sem `agent_takes` novo.
- [Integração LatentSync 2-Estágios no Talking Head](progress/changes/2026-08-07-latentsync-talking-head.md) — LTX gera vídeo base em 720p e LatentSync aplica lip-sync com a voz ElevenLabs (3 retries, sem fallback silencioso).
- [Prediction Replicate durável e falha parcial por item](progress/changes/2026-08-04-replicate-prediction-duravel.md) — criação/polling/webhook reconciliam timeout ambíguo sem POST duplicado nem encerrar os demais itens.
- [Compatibilidade de layout mono na montagem FFmpeg](progress/changes/2026-08-04-ffmpeg-layout-mono.md) — runtime Bookworm monta locução mono a 48 kHz e testa o FFmpeg da própria imagem antes do deploy.
- [Finalização de voz compatível com o contrato ElevenLabs](progress/changes/2026-08-04-elevenlabs-finalizacao-voice-description.md) — descrição server-owned válida elimina o 422 antes da criação da voz permanente.
- [Quotas pagas no ambiente local](progress/changes/2026-08-03-dev-local-quotas-pagas.md) — wrapper configura limites no PostgreSQL local sem risco de usar o Neon do host.
- [Reorganização do histórico de progresso](progress/changes/2026-08-03-reorganizacao-historico-progresso.md) — painel curto, legado mensal íntegro e contrato documental testado.
- [Integração completa ElevenLabs Voice Design](progress/archive/2026-08.md#correção--integração-completa-elevenlabs-voice-design-2026-08-03) — design, seleção e finalização de voz entraram no fluxo V2 durável.
- [Vídeo final com locução e montagem FFmpeg](progress/archive/2026-07.md#correção--vídeo-final-com-locução-elevenlabs-e-montagem-ffmpeg-2026-07-29) — finais passaram a concatenar clips e incluir áudio validado.
- [QC aceita ponteiros canônicos R2/S3](progress/archive/2026-07.md#correção--qc-aceita-ponteiros-canônicos-de-vídeo-r2s3-2026-07-29) — mídia persistida deixou de falhar no gate de integridade.

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
