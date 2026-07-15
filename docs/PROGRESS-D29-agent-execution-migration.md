# PROGRESS-D29 - Migracao para agents executions

Documento operacional especifico da migracao definida em D29.

ADR detalhado: `docs/ADR-D29-agent-executions-over-tools.md`

## Objetivo

Migrar o motor para permitir agents executions por stage, de forma incremental, sem
substituir LangGraph nem romper os contratos atuais de dry-run, UI, CLI, checkpointing,
SSE, tracing, media persistence e validacao de shape.

## Estado atual em 2026-07-14

- [x] LangGraph segue como orquestrador canonico.
- [x] `CompositeAdapter` resolve provider/adapter por papel.
- [x] Nodes de stage chamam tools tipadas em vez de chamar adapters diretamente.
- [x] `ToolContext` extrai `adapter`, `pipeline`, `run` e `run_id` do `RunnableConfig`.
- [x] Tools validam shapes de retorno (`dict`, `list[dict]`, `Artifact`, `QCResult`,
  `str`) antes de devolver para os nodes.
- [x] `TOOL_REGISTRY` existe como metadata estatica inicial.
- [x] `TOOL_REGISTRY` e contrato publico interno completo para roteamento inicial.
- [x] Catalogo de agents/models por stage/tool existe em `agents.yaml`.
- [x] Stage executor configuravel `tool`/`agent` existe.
- [x] `concepts` e `scripts` podem usar agent execution opt-in em runtime.
- [x] Stages de midia foram avaliados e seguem bloqueados para agent execution.

## Fase 1 - Foundation da camada de tools

Status: concluida.

Entregue:

- Pacote `orchestrator.tools` com tools finas para `concepts`, `scripts`, `creators`,
  `video`, `qc`, `assembly` e `upscale`.
- `ToolOutputError` para falhas claras de shape devolvido por adapter.
- Nodes em `nodes/stages.py` delegando para tools.
- Testes de delegacao, validacao de shape, trace markers e preservacao do comportamento
  de stages.

Invariantes que passam para as fases seguintes:

- Nenhum node novo deve chamar adapter concreto diretamente.
- Nenhum agent deve bypassar uma tool para escrever no estado do grafo.
- Toda saida nao trivial precisa continuar validada antes de entrar no estado.

## Fase 2 - Consolidar `TOOL_REGISTRY`

Status: concluida.

Objetivo:

Transformar `TOOL_REGISTRY` em contrato interno suficiente para roteamento agentic.

Checklist:

- [x] Expandir `ToolSpec` com campos opcionais para `target_model`, `target_agent`,
  `agent_enabled` e, se necessario, lista de capabilities.
- [x] Garantir que cada tool registrada tenha nome estavel, role, stage e descricao.
- [x] Adicionar testes que falham se uma tool usada por stage nao estiver no registry.
- [x] Documentar no ADR/progress qualquer campo novo que vire contrato.
- [x] Manter compatibilidade com o registry atual para nao quebrar imports existentes.

Entregue:

- `ToolSpec` agora inclui `function_path`, `target_model`, `target_agent`,
  `agent_enabled` e `capabilities`.
- `function_path` aponta para a funcao real da tool e e validado contra os trace markers
  (`tool.{name}`), evitando drift entre registry e implementacao.
- Helpers publicos internos: `get_tool_spec(name)`, `tool_specs_for_stage(stage)` e
  `resolve_tool_function(spec)`.
- O default de `agent_enabled` permanece `False`: a D29 ainda nao liga agent execution em
  runtime; isso fica para as fases de catalogo/executor.

Criterio de aceite:

- A suite prova que registry e tools reais nao entram em drift.
- O registry consegue responder quais tools um agent de `concepts` ou `scripts` pode
  chamar, sem inspecao manual dos modulos.

## Fase 3 - Catalogo agents/models por stage/tool

Status: concluida.

Objetivo:

Adicionar configuracao declarativa para escolher executor/model por stage sem mudar a
topologia LangGraph.

Checklist:

- [x] Definir arquivo de configuracao do catalogo, preferencialmente em `config/`.
- [x] Carregar catalogo junto das configs existentes.
- [x] Validar stages/tools desconhecidos com erro claro.
- [x] Default: modo direto `tool`, sem agents live.
- [x] `config-mock` continua deterministico e offline.

Entregue:

- Novo modulo `orchestrator.agent_catalog` com `AgentCatalog`,
  `StageExecutionSpec`, `default_agent_catalog()` e builder validado.
- `config/agents.yaml` e `config-mock/agents.yaml` declaram todos os stages em
  `executor: tool`, com tools derivadas do contrato de `TOOL_REGISTRY`.
- `load_agent_catalog(config_dir)` carrega o arquivo quando existe e cai para default
  compativel quando `agents.yaml` esta ausente.
- Runner, CLI e web injetam `agent_catalog` em `RunnableConfig.configurable`.
- `/api/integrations` preserva `stages` e adiciona `agents` serializado para consumo da
  UI/diagnostico.
- Nenhum node consulta o catalogo ainda; agent execution segue fora do runtime ate a
  Fase 4.

Criterio de aceite:

- O catalogo pode declarar `concepts` e `scripts` como candidatos a agent execution,
  mas o comportamento default continua identico ao atual.

## Fase 4 - Stage executor `tool`/`agent`

Status: concluida.

Objetivo:

Inserir uma camada fina que escolha entre chamada direta da tool e execucao agentic para
um stage habilitado.

Checklist:

- [x] Criar executor com contrato de entrada/saida igual ao node atual.
- [x] Modo `tool`: chama a tool atual e preserva comportamento existente.
- [x] Modo `agent`: recebe apenas tools permitidas pelo registry/catalogo.
- [x] Agent execution adiciona tracing proprio sem remover spans de node/tool/adapter.
- [x] Erros mantem mensagens acionaveis e nao mascaram `ToolOutputError`.

Entregue:

- Novo modulo `orchestrator.stage_executor` com `execute_stage_tool`.
- Todos os nodes que chamam tools passam pelo executor (`roster`, `concepts`, `scripts`,
  `video`, `qc`, `assembly`, `upscale`).
- O modo `tool` e passthrough e preserva o comportamento atual.
- O modo `agent` e deterministico/offline nesta etapa: adiciona trace
  `agent.stage_executor`, valida stage/tool permitido e chama a tool registrada, mantendo
  os validators da propria tool como fronteira de shape.
- **Importante (honestidade de leitura):** ate aqui o modo `agent` e passthrough +
  tracing + gating — nao ha agente, loop de LLM nem selecao de multiplas tools. Ele chama
  exatamente a mesma tool que o modo `tool`. Logo, os testes de "pilot"
  (`test_mock_pipeline_can_opt_into_agentic_concepts_and_scripts`) provam que a fiacao nao
  quebra a pipeline e que o output tem paridade, **nao** que exista comportamento agentic.
  A unica coisa que distingue `agent` de `tool` hoje e o span `agent.stage_executor` com
  metadata `executor="agent"`/`allowed_tools`/`target_model` — coberto por
  `test_stage_executor_agent_mode_emits_agent_trace_span_metadata` e
  `test_agentic_executor_declares_dedicated_trace_span`.
- Erros de stage ausente ou tool nao permitida levantam `StageExecutionError` com mensagem
  acionavel.

Criterio de aceite:

- Trocar um stage entre `tool` e `agent` nao muda o contrato observado por CLI/web.
- Checkpoint/resume continua sob LangGraph.

## Fase 5 - Piloto em `concepts` e `scripts`

Status: concluida.

Objetivo:

Validar agent execution nos stages LLM-only, onde o risco de midia e persistencia e
menor.

Checklist:

- [x] `concepts` roda em modo agentic opt-in.
- [x] `scripts` roda em modo agentic opt-in.
- [x] Outputs continuam passando pelos mesmos validators.
- [x] Testes offline cobrem modo direto e modo agentic deterministico/mockado.
- [x] UI e CLI continuam sem mudanca de contrato.

Entregue:

- `agents.yaml` pode declarar `concepts`/`scripts` com `executor: agent` e
  `agent_enabled: true`.
- Teste de pipeline mock completa cobre `concepts` e `scripts` em modo agentic opt-in,
  preservando `results`, scripts e resumo publico.
- O default de `config/` e `config-mock/` continua `executor: tool`, sem custo e sem rede.

Criterio de aceite:

- O run mock completo continua verde.
- A suite completa continua verde com cobertura exigida pelo projeto.
- O operador nao precisa mudar comandos para usar o comportamento default.

## Fase 6 - Avaliar stages de midia

Status: concluida como avaliacao/bloqueio.

Stages afetados:

- `creator`
- `video`
- `assembly`
- `upscale`

Regra:

Nenhum desses stages deve virar agentic antes de haver contrato testado para artefatos,
persistencia de midia, retries, fallback, erro visivel na UI e shape validado.

Decisao aplicada:

- `executor: agent` e `agent_enabled: true` sao aceitos apenas para `concepts` e
  `scripts`.
- Tentativas de habilitar `agent` para `video`, `roster`, `qc`, `assembly` ou `upscale`
  falham no carregamento do catalogo com erro claro.
- Esses stages continuam usando o executor em modo `tool`, preservando adapters,
  persistencia de midia, retries, fallback e erros visiveis ja existentes.

## Fase 7 - Execucao agentic real (concepts/scripts) via gateway-nativo

Status: concluida.

ADR: `docs/ADR-D31-agentic-execution.md`. Decisao: D31 em `docs/DECISIONS.md`.

Objetivo:

Introduzir comportamento agentic **real** (loop *critique -> refine* bounded) nos stages
LLM-only, com o backend real acessando o modelo **pelo AI gateway**, sem amarrar ao SDK
`anthropic`.

Entregue:

- `AgentPort` + `StageToolRunner` em `adapters/base.py`; `revision: Optional[str]` como
  canal de refino nas tools LLM (`generate_concepts`, `write_script`) e nos ports.
- `stage_executor` resolve `run_stage_agent` no adapter llm e delega o loop com `run_tool`
  (fronteira D29); adapter sem o metodo -> passthrough (`agent_backend="passthrough"`).
- `MockAdapter.run_stage_agent` + `_agent_critique` determinístico/offline (custo zero).
- **Backend real gateway-nativo:** novo `GatewayLLMAdapter`
  (`adapters/gateway_llm.py`) implementa `LLMPort` + `AgentPort` via `httpx` puro contra
  `POST {base}/chat/completions` (OpenAI-compatible; Structured Outputs por
  `response_format: json_schema`). Sem importar o SDK `anthropic`. Cliente injetavel
  (`httpx.MockTransport`) cobre tudo offline.
- Registry: `vercel_gateway_llm` -> `build_gateway_llm_adapter` (default live). O
  `AnthropicLLMAdapter` fica como legado opt-in (`anthropic`, `anthropic_sdk_gateway`).
  `config/providers.yaml` nao muda.
- Criterios de aceite do mock cobertos por teste dedicado (1 chamada quando aprova; 2 e
  output diferente quando refina).

Fora de escopo (segue o ADR): streaming de token do LLM (caminho non-streaming), stages de
midia agentic, multiplas tools por stage, loop com mais de um refinamento, e tornar agent
execution o default.

Criterio de aceite:

- Suite offline verde com cobertura 100%, incluindo `gateway_llm.py` e a camada agentic do
  adapter legado. Run mock em modo tool default segue determinístico e sem rede.

## Log de falhas investigadas

Registrar aqui toda falha encontrada durante a migracao, seguindo a regra do projeto:
sintoma -> causa raiz -> correcao -> verificacao.

Formato:

```md
### YYYY-MM-DD - titulo curto

- Sintoma:
- Causa raiz:
- Correcao:
- Verificacao:
```

### 2026-07-14 - registry sem contrato agentic completo

- Sintoma: os novos testes de contrato falharam com `AttributeError` para
  `function_path` e `ImportError` para `resolve_tool_function`/`get_tool_spec`/
  `tool_specs_for_stage`.
- Causa raiz: `TOOL_REGISTRY` ainda era apenas uma lista estatica minima
  (`name`/`description`/`role`/`stage`), suficiente para documentacao, mas insuficiente
  para roteamento agentic ou deteccao de drift com as funcoes reais usadas pelos nodes.
- Correcao: expandir `ToolSpec` com campos agentic opcionais e capabilities, adicionar
  paths resolviveis das funcoes reais, e expor helpers de lookup/resolucao mantendo
  defaults compativeis com o contrato antigo.
- Verificacao: `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_tools.py -q`
  -> 25 passed; `rtk proxy .venv/bin/python -m pytest` -> 601 passed, 2 skipped,
  cobertura 100%.

### 2026-07-14 - catalogo agentic ausente e validacao permissiva

- Sintoma: os testes RED falharam inicialmente com `ImportError` para
  `load_agent_catalog`; depois, os config-dirs oficiais falharam por ausencia de
  `agents.yaml`; por fim, `stages: []` nao levantava erro.
- Causa raiz: nao havia modulo/loader de catalogo na Fase 2; `config/` e `config-mock/`
  ainda nao tinham arquivo declarativo; e o parser usava `data.get("stages") or {}`,
  convertendo uma lista vazia invalida em mapping vazio valido.
- Correcao: criar `orchestrator.agent_catalog`, adicionar `agents.yaml` nos dois
  config-dirs, carregar/injetar o catalogo em runner/CLI/web, expor em
  `/api/integrations`, e distinguir `stages` ausente/null de tipo invalido.
- Verificacao: fatia focada de catalogo/CLI/web -> 78 passed; suíte completa
  `rtk proxy .venv/bin/python -m pytest` -> 619 passed, 2 skipped, cobertura 100%.

### 2026-07-14 - erro de parametrizacao no teste do catalogo

- Sintoma: a coleta de `tests/test_agent_catalog.py` quebrou porque um caso
  parametrizado tinha 3 valores para 2 nomes.
- Causa raiz: uma string YAML foi separada por virgula no meio do literal, criando um
  terceiro item na tupla.
- Correcao: concatenar o literal corretamente dentro do mesmo argumento `body`.
- Verificacao: `rtk proxy .venv/bin/python -m pytest --no-cov tests/test_agent_catalog.py
  -q` -> 18 passed; a fatia final de catalogo/CLI/web passou com 78 testes.

### 2026-07-14 - executor agentic inexistente e midia sem bloqueio

- Sintoma: os testes RED de Fase 4 falharam com `ModuleNotFoundError:
  No module named 'orchestrator.stage_executor'`; depois a suite completa passou
  funcionalmente, mas falhou cobertura porque o erro de stage ausente nao era exercitado.
- Causa raiz: ate a Fase 3 o catalogo era apenas dado inerte; nao havia camada de
  execucao entre nodes e tools. Alem disso, o catalogo ainda aceitava configuracoes
  ambiguas (`executor: agent` sem `agent_enabled`, `agent_enabled` com `executor: tool`)
  e permitia midia em modo agentic.
- Correcao: criar `execute_stage_tool`, integrar todos os nodes a ele, adicionar modo
  agentic deterministico/offline para tools permitidas, validar combinacoes de
  `executor`/`agent_enabled`, e bloquear `agent` fora de `concepts`/`scripts`.
- Verificacao: fatia focada de executor/catalogo/tools/builder/CLI/web -> 112 passed;
  suite completa `rtk proxy .venv/bin/python -m pytest` -> 629 passed, 2 skipped,
  cobertura 100%; `rtk proxy .venv/bin/python -m compileall -q src tests` -> OK.

### 2026-07-15 - endurecimento da camada D29 (findings de code review)

- Sintoma: code review da branch apontou 4 fragilidades na camada nova (nenhum crash,
  mas robustez/altitude/reuso): (1) catalogo mal-tipado em `configurable.agent_catalog`
  caia silenciosamente no default tool-mode; (2) o gate "agent so em concepts/scripts"
  valia apenas no load do YAML, nao em runtime; (3) o `except Exception` best-effort de
  assembly/upscale engolia `StageExecutionError` (erro de config) como falha por-item;
  (4) `default_agent_catalog` reimplementava o agrupamento stage->tools que o registry ja
  expunha.
- Causa raiz: invariantes e validacoes concentradas no loader/nos call sites felizes,
  sem defesa em runtime; e duplicacao de logica de agrupamento.
- Correcao: (A1) `_catalog_from_config` distingue ausente (default) de tipo invalido
  (levanta `StageExecutionError`); (A2) helper unico `is_agent_stage_allowed` +
  `agent_stage_not_allowed_message` em `agent_catalog.py`, usado tanto no
  `build_agent_catalog` quanto no branch agent do `execute_stage_tool`; (A3) `node_assembly`
  e `node_upscale` reraise `StageExecutionError` antes do `except Exception`; (A4)
  `default_agent_catalog` reusa `tool_specs_for_stage`.
- Verificacao: 5 testes novos (RED->GREEN) em `test_stage_executor.py` e
  `test_stages_coverage.py`; suite completa `rtk proxy .venv/bin/python -m pytest` ->
  637 passed, 2 skipped, cobertura 100%; run mock
  `orchestrator run --batch 4 --offer "serum X" --config-dir config-mock` -> 4/4 aprovados,
  deterministico, custo zero; `compileall -q src tests` -> OK.

### 2026-07-15 - Fase 7: dublê de teste e cobertura do caminho agentic

- Sintoma: `tests/test_stages_coverage.py::test_node_scripts_writes_script_per_concept`
  falhava com `TypeError: write_script() got an unexpected keyword argument 'revision'`;
  a suite tambem ficava a 99.57% (abaixo do gate de 100%).
- Causa raiz: a Fase 7 adicionou `revision` ao `LLMPort.write_script` e a tool
  `write_script_tool` passou a sempre repassar `revision=revision`, mas o adapter-fake do
  teste tinha a assinatura pre-Fase-7. Alem disso, os branches `revision` e a camada
  `run_stage_agent`/`_agent_critique` (adicionados nesta branch tanto no mock quanto no
  adapter LLM real) nao tinham cobertura.
- Correcao: alinhar o fake ao port (`revision=None`); criar `GatewayLLMAdapter`
  gateway-nativo com testes offline (`tests/test_gateway_llm.py`, `httpx.MockTransport`);
  cobrir os branches `revision`/agentic do adapter legado em `tests/test_anthropic_llm.py`;
  adicionar teste dedicado do criterio de aceite do mock em `tests/test_stage_executor.py`;
  repontar `vercel_gateway_llm` no registry (ajustando o assert em
  `tests/test_registry_composite.py` para `GatewayLLMAdapter`).
- Verificacao: `rtk proxy .venv/bin/python -m pytest` -> 673 passed, 2 skipped, cobertura
  100.00%; run mock `orchestrator run --batch 4 --offer "serum X" --config-dir config-mock`
  -> 4/4 aprovados, determinístico, custo zero; `compileall -q src tests` -> OK.

## Comandos de verificacao esperados

Documentacao apenas:

```bash
rtk git diff -- docs
```

Mudancas de codigo em fases futuras:

```bash
rtk proxy python -m pytest
```

Quando houver frontend afetado:

```bash
cd front && rtk npm run build
```
