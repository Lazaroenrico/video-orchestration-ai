# Pipeline V2: contrato de produto

## Objetivo

A experiência pública tem cinco fases. LangGraph continua executando nodes internos,
mas a UI e a API mostram apenas o trabalho que ajuda o usuário a tomar uma decisão.

1. **Configuração**
2. **Plano criativo**
3. **Revisão**
4. **Produção e QC**
5. **Montagem**

Distribuição não faz parte deste produto. Um run aprovado termina em `assembled`.

## O que o usuário informa

O usuário só preenche dados na Configuração e decide na Revisão. Não há confirmação
entre agents, escolha de modelo, tier, provider, prompt ou parâmetro técnico.

### Configuração

Campos obrigatórios:

- **Produto e oferta:** “O que está sendo vendido, preço ou condição da oferta e
  benefício principal comprovado.”
- **Público:** “Quem deve comprar, qual problema enfrenta e em qual situação usaria o
  produto.”

Campos opcionais:

- **Fatos e restrições:** “Fatos comprovados, claims permitidos, requisitos legais e
  assuntos que não podem aparecer.”
- **Direção dos creators:** “Perfil, aparência, voz, energia, figurino ou cenário
  desejado.”
- **Direção dos vídeos:** “Demonstração, enquadramento, ritmo, cenário e referências
  visuais.”
- **Performance anterior:** JSON no contrato `PerformanceSnapshot`.

Seletores:

- plataforma: TikTok, Instagram, YouTube ou Facebook;
- objetivo: conversão, awareness ou consideração;
- quantidade de pacotes criativos: 1 a 48.

### Plano criativo

Não solicita entrada. A interface mostra progresso objetivo:

- `Criando conceitos`;
- `Escrevendo roteiros: 4/12`;
- `Definindo creators`;
- `Gerando previews: 1/2`.

O sistema sempre prepara exatamente dois creators. Concepts, scripts e perfis são
agents criativos; geração de imagem/voz, vídeo, QC e montagem são adapters automáticos.

### Revisão

Uma única tela apresenta os dois creators e cada pacote com conceito + roteiro.

O usuário escolhe uma ação:

- **Aprovar e produzir:** pode enviar edições nos conceitos, roteiros e creators.
- **Regenerar conceitos:** informa feedback geral e, opcionalmente, IDs.
- **Regenerar roteiros:** informa feedback geral e, opcionalmente, IDs.
- **Regenerar creators:** informa feedback geral.

Template de feedback:

> O que deve mudar, por quê e qual resultado deve ser preservado. Não inclua
> instruções técnicas, nomes de modelos ou prompts.

Após aprovação não há novo gate. Produção, QC, retries e montagem são automáticos.

## Contratos da API

- `POST /api/v2/runs` recebe `{"campaign": CampaignInput}`.
- `GET /api/state/{run_id}` retorna fase, revisão pendente, progresso e atividade.
- `GET /api/runs` separa `active`, `errored` e `cancelled`.
- SSE em `/api/stream/{run_id}` emite `progress_event` e `awaiting_review`.
- `POST /api/v2/runs/{run_id}/review` recebe `approve` ou `regenerate`.

Runs duráveis devem reenviar `gate_id`, `version` e
`gate_type=review_creative_plan`. Versão stale retorna 409; gate cancelado retorna 410.

## O que não é mostrado

System prompts, mensagens internas, chain-of-thought, paths de prompt, provider config,
credenciais e payloads brutos não fazem parte do contrato público. Observabilidade usa
somente fase, stage, contadores, `prompt_version`, `prompt_hash`, IDs operacionais,
tentativas, custo e erro sanitizado.

## LangChain e LangGraph

LangGraph controla ordem, estado, fan-out, loop de QC, interrupt e resume. LangChain
formata chamadas de modelo e tool calling. Nenhum deles limita a experiência pública:
a limitação é deliberada nos contratos do produto.

O agent só pode chamar a tool da sua fase uma vez e precisa enviar um objeto válido.
Media e QC não usam agent loop. Isso evita conversas internas, múltiplas decisões
ocultas e custo imprevisível.

## Estados e cancelamento

Fases persistidas: `running`, `review`, `done`, `error`, `cancelled`, além dos estados
V1 mantidos apenas para leitura/migração. A migração `20260728_0009` cancela gates V1
pendentes, marca runs/jobs associados como `cancelled` e impede resolução posterior.
