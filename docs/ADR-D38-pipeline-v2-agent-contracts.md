# ADR-D38: pipeline V2 e contratos seguros de agents

- **Data:** 2026-07-28
- **Status:** aceito
- **Substitui:** topologia pública e decisões agentic de D31, D33 e D35

## Contexto

A pipeline expunha nodes técnicos, dois gates humanos e atividade sem indicação clara
do processo atual. Persona e vídeo também podiam entrar no agent loop. Os contratos de
saída eram permissivos e o tracing podia conter prompt/copy. Isso tornava a experiência
confusa e ampliava autoridade, custo e superfície de prompt injection.

## Decisão

### Topologia pública

Expor cinco fases: Configuração, Plano criativo, Revisão, Produção e QC, Montagem.
Internamente:

`concepts → scripts → creator_profiles → roster → review → Send(process_item) → feedback`

Existe um único interrupt, `review_creative_plan`. Regeneração volta somente ao alvo
pedido; aprovação inicia fan-out automático. Exatamente dois creators são criados.

### Fronteira de agent

Somente `concepts`, `scripts` e `creator_profiles` podem usar `executor: agent`.
Persona sai do grafo público. Roster/media, vídeo, QC, assembly e upscale são tools ou
adapters determinísticos.

Cada agent possui:

- system prompt de fase versionado;
- política compartilhada imutável;
- uma allowlist com uma tool;
- um schema terminal `creative-v2`;
- no máximo uma submissão de tool;
- output revalidado por Pydantic com `extra="forbid"`;
- IDs e associações materializados pelo servidor.

### Prioridade e prompt injection

A autoridade efetiva é:

1. controles server-side;
2. política compartilhada;
3. contrato da fase;
4. campanha, performance, outputs anteriores, resultados de tool e feedback.

O nível 4 é sempre dado não confiável. É serializado em bloco
`UNTRUSTED_STAGE_DATA`, nunca concatenado ao system prompt. Texto que imite role,
system message, XML, policy ou tool call permanece dado.

O prompt não pode alterar IDs, contagem, schema, routing, budget, provider, segurança
ou tool allowlist. A defesa principal é estrutural no executor, não apenas textual.

### Sigilo e observabilidade

O catálogo público e traces expõem somente `has_system_prompt`, `prompt_version`,
`prompt_hash` e `schema_version`. Corpo/path do prompt, mensagens, campanha, offer,
scripts e creative inputs são omitidos incondicionalmente. A flag de redação não pode
reabilitar esses campos.

Progresso é projetado pelo backend para REST/SSE. Eventos customizados informam
unidades concluídas, por exemplo scripts `4/12` e previews `1/2`.

### Cancelamento V1

A revisão `20260728_0009` adiciona `review`/`cancelled`, cancela todo gate pendente no
momento da migração e cancela run/job associado. `resolve_gate` distingue cancelado
(410) de stale (409). A operação de repositório é tenant-scoped e idempotente.

## Consequências

- O usuário fornece briefing uma vez e toma uma decisão uma vez.
- Agents têm tarefas menores, contratos verificáveis e custo limitado.
- LangChain continua útil para tool calling e provider abstraction, sem definir UX.
- LangGraph mantém resume, fan-out e QC, sem expor a topologia interna.
- Prompts continuam revisáveis no repositório por operadores, mas não são API pública.
- Clientes V1 podem ser mantidos apenas como compatibilidade; novos fluxos usam API V2.

## Critérios de aceite

- API V2 rejeita campos desconhecidos.
- Cada agent aceita apenas sua tool e seu schema.
- Dois creators e cobertura exata das assignments.
- Um único `awaiting_review`.
- Regeneração volta à fase correta; aprovação produz.
- REST/SSE mostram cinco fases e contadores granulares.
- Prompt/copy não aparece em API de catálogo nem tracing.
- Migração cancela gates V1 e resolução posterior retorna 410.
