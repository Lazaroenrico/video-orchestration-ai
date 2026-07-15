# ADR-D31 - Execucao agentic real em concepts/scripts (Fase 7 do D29)

Data: 2026-07-15

Status: implementado (2026-07-15)

Atualizacao (2026-07-15): o backend real e um adapter LLM **gateway-nativo**
(`GatewayLLMAdapter`, `src/orchestrator/adapters/gateway_llm.py`) que fala com o Vercel AI
Gateway por `httpx` puro contra `POST {base}/chat/completions` (OpenAI-compatible,
Structured Outputs via `response_format: json_schema`) — **sem** o SDK `anthropic`. O
provider default `vercel_gateway_llm` aponta para ele; o `AnthropicLLMAdapter` (SDK) fica
como legado opt-in (`anthropic`, `anthropic_sdk_gateway`). Ver D31 em `docs/DECISIONS.md`.
Streaming de token do LLM fica fora desta fase (caminho non-streaming).

Relacionadas: `docs/ADR-D29-agent-executions-over-tools.md`, `docs/DECISIONS.md` (D29),
`docs/PROGRESS-D29-agent-execution-migration.md`.

## Contexto

A D29 entregou a fiacao completa `LangGraph node -> stage executor -> typed tool ->
CompositeAdapter -> concrete adapter`, com catalogo declarativo (`agents.yaml`), gating
de stages e span de tracing `agent.stage_executor`. Porem, ate a Fase 6, o modo `agent`
era **passthrough + tracing + gating**: `_execute_agentic_tool` chamava exatamente a mesma
tool que o modo `tool`, sem agente, loop de LLM ou refinamento. Nao havia comportamento
agentic — so observabilidade e validacao de configuracao.

Esta ADR define a Fase 7: introduzir comportamento agentic **real** para os stages
LLM-only (`concepts`, `scripts`), sem quebrar nenhum contrato do D29.

## Decisao

Adotar um loop agentic **bounded** no padrao *critique -> refine*, de dono no adapter LLM,
exposto por um novo port `AgentPort.run_stage_agent`.

Fluxo do loop (uma passada de refinamento no maximo):

1. `draft = await run_tool(**inputs)` — gera o rascunho pela **typed tool** (validada).
2. `revision = critique(stage, draft)` — o agente avalia o rascunho e decide se pede
   melhoria; o brain e o modelo LLM (real via **AI gateway**) ou um heuristico
   deterministico (mock offline).
3. Se `revision` for vazio, retorna o `draft`. Senao,
   `return await run_tool(**inputs, revision=revision)` — regenera com a diretiva.

Fronteiras (herdadas do D29, inegociaveis):

- O agente **so** chama tools tipadas: `run_stage_agent` recebe um `run_tool` que encapsula
  `tool_fn(ctx, ...)`. O agente **nunca** chama metodos de dominio do adapter diretamente;
  toda saida passa pelos validators da tool (`orchestrator.tools.base`).
- `CompositeAdapter` continua a fonte de roteamento por papel; `run_stage_agent` e delegado
  ao adapter do papel `llm`, e so existe quando esse adapter o implementa (senao o executor
  cai em passthrough puro).
- Gating de stage vale em runtime: agent execution so em `concepts`/`scripts`.
- Determinismo offline: o mock deriva tudo de hash; `config-mock` continua `executor: tool`
  por default (sem custo, sem rede).

### Canal de refinamento: `revision`

O refinamento generico e um parametro opcional `revision: Optional[str] = None` adicionado
as tools LLM (`generate_concepts`, `write_script`) e aos metodos de adapter correspondentes.

- `revision is None` (default) => comportamento **identico** ao atual. Backward-compatible;
  o modo `tool` nunca passa `revision`, entao nao muda nada fora do modo agent.
- `revision` setado => o adapter incorpora a diretiva na geracao (no mock, dobrada no hash
  para gerar um output distinto e deterministico; no gateway, anexada ao prompt).

## Arquitetura

```
node -> execute_stage_tool (agent) -> adapter.run_stage_agent(run_tool, inputs)
                                          |-> run_tool(**inputs)            -> typed tool -> adapter.generate_*
                                          |-> critique(draft)               -> LLM (gateway) | heuristico (mock)
                                          |-> run_tool(**inputs, revision)  -> typed tool -> adapter.generate_*
```

O `execute_stage_tool` no modo `agent`:

- resolve `run_stage_agent = getattr(ctx.adapter, "run_stage_agent", None)`;
- se `None` (adapter LLM sem capacidade agentic, ex.: futuros adapters), faz passthrough e
  marca `agent_backend="passthrough"` no trace;
- se presente, delega o loop, passando `run_tool` (fronteira validada), `spec.tools`,
  `inputs=kwargs` e `spec.target_model`.

O span `agent.stage_executor` e a metadata (`executor="agent"`, `allowed_tools`,
`target_model`, `agent_backend`, `agent_revised`) permanecem.

## Contratos preservados

- CLI, web, SSE, `/api/state`, reports do runner: inalterados.
- Modo `tool` (default) byte-identico ao atual.
- Validacao de shape permanece nas tools.
- Tracing separado por node/tool/adapter; o agent entra como span adicional.
- Checkpoint/resume seguem sob LangGraph.

## Fora de escopo

- Stages de midia agentic (`creator`, `video`, `assembly`, `upscale`) — seguem bloqueados.
- Multiplas tools por stage / selecao de ferramentas pelo agente (hoje cada stage LLM tem
  uma unica tool; o contrato ja aceita `allowed_tools` para o futuro).
- Loop com mais de uma passada de refinamento — bounded a um refinamento nesta fase.
- Tornar agent execution o default; `config-mock` continua `tool`.

## Riscos e mitigacoes

- **Trabalho desperdicado (refino que nao muda nada):** `revision` e dobrada na geracao, o
  output refinado e materialmente diferente; o mock so refina quando o critique retorna
  diretiva.
- **Perda de determinismo:** o critique do mock e puramente deterministico (hash do draft);
  o backend real fica atras de `--live` opt-in.
- **Bypass de validacao:** o agente so chama `run_tool` (typed tool validada).
- **Custo:** um refinamento adiciona no maximo uma chamada LLM extra por item; bounded e
  opt-in por `executor: agent`.

## Criterios de aceite

- Mock: o modo agent executa >= 2 chamadas de tool quando o critique pede refino, e o
  output refinado difere do rascunho; quando o critique aceita, faz 1 chamada e retorna o
  rascunho.
- O output agentic continua passando pelos validators das tools.
- Run mock completo em modo agent opt-in preserva `results`, scripts e resumo publico.
- Backend real acessa o modelo **pelo AI gateway**, coberto offline via cliente injetavel.
- Suite offline verde com cobertura 100%; `config-mock` sem chamadas externas.
