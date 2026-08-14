# Runtime de linguagem nativo LangChain

Data: `2026-08-14`

## Resultado

Foi introduzida a composição central `RunDependencies` com `LanguageRuntime` e
adapters de domínio, conectada ao runner e ao caminho HTTP local. Models nativos
LangChain, agents criativos schema-first e mock determinístico estão disponíveis;
vídeo passa artifact diretamente sem nova proveniência `agent_takes`. A CLI deixou
de expor `run`, `loop`, `status`, `resume` e `list`; `runner` agora exige `--once`.

## Mudanças de contrato

O contrato interno de `RunnableConfig.configurable` ganhou `language_runtime`, sem
alteração nos contratos REST/SSE ou em `BatchState`. A decisão canônica é
[ADR-D46](../../ADR-D46-langchain-native-language-runtime.md).

## RED → GREEN

- **RED:** o tracer bullet do `LanguageRuntime` não importava o módulo; a factory e
  o modelo mock ainda não existiam.
- **GREEN:** `LanguageRuntime.from_provider("mock", {})` produz o mesmo conteúdo
  para a mesma entrada; o teste direcionado passou.
- **RED:** o caminho agentic mock falhou com `KeyError('llm')` ao consultar o
  composite domain-only antes do runtime.
- **GREEN:** o executor exige `LanguageRuntime` para qualquer stage agent e não
  mantém passthrough silencioso.
- **RED:** a saída estruturada mock de script tinha 21 palavras e violava o budget
  server-owned de 28 palavras.
- **GREEN:** o materializador mock gera um draft determinístico que respeita o
  budget; o pipeline agentic direcionado passou.
- **RED:** a primeira suíte direcionada ainda esperava os comandos de campanha e
  os adapters LLM removidos.
- **GREEN:** testes foram substituídos por contratos públicos: `runner --once`,
  catálogo domain-only, materialização nativa e saída `Artifact` direta.
- **RED:** a migração do roster fixou dois perfis, mas assignments ainda usavam
  índices de todos os conceitos.
- **GREEN:** assignments usam `index % 2`; o fluxo E2E de gate offline voltou a
  passar.
- **RED:** middleware de budget ficava vazio porque lia uma chave inexistente,
  usava `tool_call_limit` e engolia `TypeError`; materialização ainda dependia
  de schemas JSON duplicados no registry.
- **GREEN:** budgets agora resolvem `by_stage > global > default`, usam `run_limit`
  com `exit_behavior="error"`, distinguem o cache por prompt/model/budgets e
  validam submissions contra os modelos Pydantic canônicos.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `KeyError('llm')` em agentic mock | `CompositeAdapter` domain-only era consultado antes da dependência de linguagem | Reordenado o executor para usar `LanguageRuntime` primeiro. |
| Script mock rejeitado no budget | Draft determinístico curto demais | Ajustado apenas o payload mock estruturado, preservando validação. |
| `uv lock` sem temporário | Cache do uv fora da raiz gravável | Executado com a permissão aprovada para atualizar `uv.lock`. |
| Roster E2E excedia dois perfis / assignment fora do índice | Materializador usava o tamanho do batch como número de creators | Roster fixado em dois perfis e assignments roteados por módulo 2. |
| Revisão independente encontrou roster aceitando 1–48 perfis e `creator_index=48` | O schema Pydantic havia relaxado uma contagem server-owned e permitia índice inexistente | Restaurado o contrato de exatamente dois perfis e índices `0..1`; regressões cobrem 0, 1, 3 e 48 perfis e índice 2. |
| Asserções de prompt de imagem divergiam | Prompt seguro existente foi refinado por alteração preexistente | Teste atualizado para as frases canônicas atuais, sem reverter o prompt. |
| Teste de gate desconhecido monkeypatchava builder removido | O setup HTTP ainda dependia do seam antigo do `CompositeAdapter` | Teste passou a injetar `RunDependencies.build`, preservando a asserção de rejeição. |
| Agents sem limites efetivos | Chave de configuração incorreta e argumento incompatível do middleware eram silenciados | Resolver budgets do pipeline sem fallback silencioso e instanciar os middlewares com a assinatura instalada. |
| Registry mantinha schemas de tool duplicados | `ToolSpec.parameters` era uma segunda fonte de verdade | Remover schemas JSON e validar/materializar com `agent_output_model` Pydantic. |
| Teste integrado de `create_agent` não terminava após o model responder | Nesta combinação LangChain/LangGraph, `wrap_model_call` com `ToolStrategy` ficava pendente depois do structured output | Substituído por um decorador estreito de `BaseChatModel` que força `parallel_tool_calls=false` em `bind_tools`; o teste integrado real passou em 2,4 s. |
| `test_run_cycles_chains_feedback` falhava com `AttributeError` | A remoção do comando Click `loop` apagou também o serviço interno `runner.run_cycles`, fora do escopo aprovado | Restaurado o serviço interno e mantida somente a superfície CLI removida. |
| Resume parcial e testes de prompt falhavam com `audio_uri` inesperado | `FlakyAdapter` e spies locais ainda implementavam a assinatura anterior do `VideoPort` | Atualizados os test doubles para encaminhar o argumento opcional, sem alterar as asserções de resumibilidade ou prompt. |
| Routing esperava `voiceover` depois de QC | A topologia atual sintetiza a voz antes do talking-head para lip-sync e, após aprovação de QC, segue direto à montagem | Atualizada a asserção obsoleta para `assembly`, preservando o contrato vigente do grafo. |
| Teste de system prompt referenciava `_MockAgentBrain` removido | O caso preservado de propagação do prompt ainda estava acoplado ao loop custom abolido | Reescrito contra `LanguageRuntime.agent_for`/`create_agent`, mantendo a mesma garantia de separação do system prompt. |
| Caso sem gênero ainda esperava apresentação `neutral` | O contrato atual garante apresentação vocal concreta e determinística; sem pista e sem ID, o índice zero resulta em `feminine` | Corrigida a expectativa obsoleta do teste sem alterar a derivação de produção. |

## Verificação final

- `uv run python -m pytest tests/test_language_runtime.py -q`: asserção passou;
  o comando isolado reporta cobertura global abaixo de 100% por causa do `addopts`.
- `uv run python -m pytest tests/test_creative_plan_graph.py --no-cov -q`: 4 passed.
- `uv run python -m pytest tests/test_stage_executor.py::test_mock_pipeline_can_opt_into_agentic_concepts_and_scripts --no-cov -q`: passed.
- `uv run python -m pytest tests/test_video_agent_node.py --no-cov -q`: 11 passed.
- `python -m compileall -q src/orchestrator`: passou.
- `rtk proxy .venv/bin/python -m pytest tests/test_language_runtime.py tests/test_stage_executor.py tests/test_registry_composite.py tests/test_cli.py --no-cov -q`: 20 passed.
- Testes de budgets e schemas canônicos (`test_language_runtime.py`,
  `test_stage_executor.py`, `test_tools.py`, `test_agent_catalog.py`,
  `test_creative_plan_graph.py`): passaram integralmente.
- Conjunto ampliado anterior incluindo endpoints web: passou integralmente.
- Conjunto ampliado de runtime, CLI, vídeo, segurança, tools, creative plan, progresso
  e endpoints web: passou integralmente.
- `rtk proxy .venv/bin/python -m pytest tests/test_approval_gate.py --no-cov -q`: 7 passed.
- `rtk proxy .venv/bin/python -m pytest --no-cov -q --ignore=<testes PostgreSQL>`:
  execução avançou além de 37%; foi interrompida pelo tempo dos testes longos,
  sem nova falha funcional após os ajustes direcionados.
- `rtk proxy .venv/bin/python -m compileall -q src`: passou; `ruff check` dos
  arquivos alterados: passou.
- `rtk proxy .venv/bin/python -m pytest tests/test_creative_contracts.py tests/test_language_runtime.py tests/test_stage_executor.py --no-cov -q -x`: 38 passed após o parecer independente.
- A suíte global alcançou o primeiro teste PostgreSQL e falhou no fixture por
  ausência de servidor em `127.0.0.1:5432`, bloqueio de infraestrutura conhecido.

## Pendências ou bloqueios externos

O único bloqueio restante é a infraestrutura PostgreSQL local ausente; adapters
pagos/live não foram executados.
## Correções posteriores de aceite (2026-08-14)

- **Sintoma → causa → correção:** gateway Anthropic produzia `/v1/v1/messages`;
  `_gateway_url` era compartilhado incorretamente com OpenAI → helper Anthropic
  separado remove apenas o sufixo final `/v1`, com testes offline de base/model/retry.
- **Sintoma → causa → correção:** pré-bind gerava `RunnableBinding` cedo demais e o
  primeiro middleware de `model_settings` travava após o structured output → um
  decorador `BaseChatModel` preserva o lifecycle nativo e força
  `parallel_tool_calls=false` somente quando `create_agent` chama `bind_tools`.
- **Sintoma → causa → correção:** projector ignorava `AIMessage`/`AIMessageChunk` e
  podia expor blocos estruturados → extração de usage percorre mensagens/ChatResult e
  tokens aceitam somente texto simples; testes cobrem deduplicação, tentativas e leaks.
- **Sintoma → causa → correção:** auditoria encontrou testes puros removidos junto com
  adapters abolidos → casos de normalização, custo, metadata, tracing, CLI operacional,
  registry de domínio e marcadores de adapters foram restaurados/adaptados; somente
  contratos explicitamente removidos continuam excluídos.
- **Validação RED → GREEN:** o repro integrado travava deterministicamente após
  `bind_tools`/`_agenerate`; sem `wrap_model_call` terminava normalmente. Após o
  decorador de model, `create_agent.ainvoke` retornou `structured_response`, confirmou
  `parallel_tool_calls=false` no binding e a matriz runtime/projector/custos fechou em
  36 testes aprovados.
