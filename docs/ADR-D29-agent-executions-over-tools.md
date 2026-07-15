# ADR-D29 - Migracao incremental para agents executions sobre tools

Data: 2026-07-14

Status: aceito para execucao incremental

ADR resumida relacionada: `docs/DECISIONS.md`, D29.

## Contexto

O motor atual usa LangGraph como runtime de orquestracao da pipeline de AI UGC. Ele ja
resolve responsabilidades criticas: fan-out paralelo por item, conditional edges para
tier/QC, interrupts humanos, checkpointing SQLite async, resume por `thread_id`, SSE da
UI e compatibilidade CLI/web.

A base mais recente introduziu `orchestrator.tools` como uma camada fina entre nodes e
adapters. Os nodes constroem um `ToolContext` a partir do `RunnableConfig`, chamam tools
tipadas, e as tools validam o shape devolvido pelos adapters antes de passar dados para o
grafo. O adapter real continua sendo o `CompositeAdapter`, resolvido por papel a partir
de `providers.yaml`.

Migrar diretamente para um runtime agentic completo neste ponto teria blast radius alto:
quebraria ou duplicaria responsabilidades ja cobertas por LangGraph, colocaria em risco
o dry-run deterministico, e dificultaria manter gates humanos, persistencia de midia e
resumibilidade.

## Decisao

A migracao para agents executions sera incremental. LangGraph permanece como runtime
canonico do fluxo no primeiro ciclo da migracao, e agents futuros serao introduzidos
como executores internos de stages especificos, sempre atraves da camada de tools.

As fronteiras estaveis sao:

- Agents chamam tools tipadas; agents nao chamam adapters diretamente.
- Tools continuam validando output e traduzindo erro de shape em erro claro.
- `CompositeAdapter` continua sendo a fonte de roteamento provider/adapter por papel.
- LangGraph continua dono de topologia, checkpointing, resume, interrupts, fan-out,
  gates humanos, loop de QC e integracao CLI/web.
- `config-mock` continua offline, deterministico e sem custo.

## Arquitetura alvo

A arquitetura alvo adiciona uma camada fina de agent execution entre nodes e tools
somente para stages habilitados por configuracao. O fluxo continua:

`LangGraph node -> stage executor -> typed tool -> CompositeAdapter -> concrete adapter`

No modo atual, o stage executor e direto: chama a tool uma vez. No modo agentic, o stage
executor pode criar uma execucao de agent para decidir como chamar uma ou mais tools
permitidas para aquele stage, mas o contrato de entrada/saida do node nao muda.

O primeiro catalogo de roteamento deve ser derivado de `TOOL_REGISTRY` e expandido sem
quebrar compatibilidade. O registro deve descrever, no minimo:

- `name`: nome estavel da tool.
- `role`: papel usado pelo `CompositeAdapter`.
- `stage`: stage LangGraph associado.
- `description`: finalidade operacional.
- `function_path`: caminho importavel da funcao real da tool.
- `capabilities`: capacidades declarativas da tool para filtragem futura.
- `target_model` ou `target_agent`: quando houver preferencia explicita de execucao.
- `agent_enabled`: default falso no live e no mock ate o stage ser validado.

`function_path` e tratado como contrato: ele precisa resolver para a funcao real e bater
com o trace marker `tool.{name}`. Isso permite detectar drift entre nodes, tools e
registry antes de introduzir o executor agentic.

O catalogo configuravel vive em `agents.yaml` dentro de cada config-dir. A ausencia do
arquivo e compativel: todos os stages caem em `executor: tool`, com as tools derivadas de
`TOOL_REGISTRY`. Na Fase 3, esse catalogo e carregado e injetado em
`RunnableConfig.configurable["agent_catalog"]`, mas nenhum node o consulta ainda.
Executar agents em runtime e responsabilidade da Fase 4.

A Fase 4 introduz `orchestrator.stage_executor.execute_stage_tool` como a fronteira
runtime entre nodes e tools. No modo `tool`, a chamada e direta. No modo `agent`, o
executor adiciona tracing proprio, valida que a tool esta permitida pelo catalogo e chama
a mesma tool tipada, preservando os validators. O piloto runtime fica restrito a
`concepts` e `scripts`; stages de midia permanecem bloqueados para `agent` no carregamento
do catalogo.

## Ordem de migracao

1. Consolidar `TOOL_REGISTRY` como contrato publico interno.
2. Criar catalogo agents/models por stage/tool, carregado por configuracao.
3. Introduzir stage executor fino com dois modos: `tool` e `agent`.
4. Pilotar `agent` apenas em stages LLM-only: `concepts` e `scripts`.
5. Manter stages de midia (`creator`, `video`, `assembly`, `upscale`) adapter-driven ate
   existir contrato agentic testado para cada um.
6. So depois avaliar ampliacao do runtime agentic para mais stages ou revisao da
   topologia.

## Contratos preservados

- CLI e web nao mudam como parte da D29.
- `RunRequest`, eventos SSE, snapshots de `/api/state` e reports do runner permanecem
  compativeis.
- Checkpoints antigos continuam legiveis.
- Cassettes e testes offline continuam deterministas.
- O tracing deve permanecer separado por node, tool e adapter. Quando houver agent, ele
  entra como span adicional, nao substitui os spans existentes.
- Validacao de shape permanece nas tools. Um agent nao pode passar dados nao validados
  diretamente ao grafo.

## Fora de escopo

- Substituir LangGraph por completo.
- Remover `CompositeAdapter`.
- Fazer live agents o default.
- Mudar contratos publicos da CLI, web ou API.
- Introduzir integracoes externas novas como parte da documentacao da migracao.
- Tornar stages de midia agentic antes de haver contratos e testes especificos.

## Riscos e mitigacoes

- **Perda de determinismo:** manter `config-mock` em modo `tool` por default e exigir
  testes offline para qualquer modo `agent`.
- **Bypass de validacao:** agents so recebem tools registradas; outputs seguem pelos
  validators em `orchestrator.tools.base`.
- **Confusao de roteamento:** `CompositeAdapter` permanece a unica fonte de provider por
  papel; catalogo de agent/model nao substitui `providers.yaml`.
- **Regressao de resumibilidade:** LangGraph continua dono do checkpoint. Agent
  execution deve ser idempotente no nivel do node/stage.
- **Observabilidade fragmentada:** adicionar spans de agent sem remover spans de
  node/tool/adapter.

## Alternativas rejeitadas

- **Trocar LangGraph por um runtime agentic agora.** Rejeitado pelo risco sobre
  checkpointing, gates humanos, fan-out e UI.
- **Agents chamando adapters diretamente.** Rejeitado porque duplicaria validacao,
  quebraria o roteamento por papel e tornaria os contratos menos testaveis.
- **Configurar agents live por default.** Rejeitado porque o v1 precisa preservar dry-run
  deterministico, custo zero e testes offline.
- **Comecar por stages de midia.** Rejeitado porque esses stages dependem de artefatos,
  persistencia local/remota, shape mais rigido e falhas externas mais caras.

## Criterios de aceite da primeira etapa

- `TOOL_REGISTRY` exposto como contrato documentado e testado.
- Catalogo de agents/models carregavel sem mudar a topologia do grafo.
- `concepts` e `scripts` conseguem alternar entre executor direto e executor agentic por
  configuracao, mantendo o mesmo output observado por CLI/web.
- Suíte offline continua verde com cobertura exigida pelo projeto.
- `config-mock` permanece sem chamadas externas.
