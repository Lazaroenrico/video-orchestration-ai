# ADR-D46: runtime de linguagem nativo LangChain

Data: `2026-08-14`  
Status: aceito

## Decisão

Reafirmamos D6: LangGraph continua sendo o único dono da orquestração, estado,
checkpoints, resume, interrupt do gate humano e fan-out. LangChain é o runtime
nativo de modelos e agents criativos; LangSmith permanece a camada de tracing.
D38 continua canônica para os contratos V2 e para os três agents (`concepts`,
`scripts` e `creator_profiles`).

`RunDependencies` é a composição única por run, compartilhada por API local,
runner e worker. Ela contém um `LanguageRuntime` para modelos/Runnables LangChain
e um `CompositeAdapter` somente de domínio/mídia. A referência é transportada em
`RunnableConfig.configurable`; `BatchState` e contratos REST não mudam.

Providers de linguagem são resolvidos exclusivamente pelo `LanguageRuntime`:
`vercel_gateway_llm` usa `ChatOpenAI`, `anthropic` usa `ChatAnthropic` direto,
`anthropic_sdk_gateway` usa `ChatAnthropic` no gateway com base raiz (sem `/v1`,
que o SDK acrescenta em `/v1/messages`) e
`mock` usa Runnables determinísticos sem rede. Cada model tem uma única política
de retry; agentes são stateless e sem checkpointer interno.

Os agentes criativos usam `create_agent` + `ToolStrategy` com schemas Pydantic
produzíveis pelo modelo. IDs, contagens, assignments, provider, budgets e
segurança permanecem server-owned e a materialização continua em typed tools.
`parallel_tool_calls` fica desativado e limites de model/tool terminam com erro.

## Decisões substituídas e preservadas

As partes aplicáveis de D16–D18, D29, D31, D32 e D34 que definiam transporte,
ports ou loops próprios de LLM/agentes ficam substituídas por esta decisão. D33
já havia sido substituída por D38. D7, D8, persistência, mídia/QC/storage/effects
e seus contratos permanecem preservados. O Judge, `judge.yaml` e cassettes estão
fora deste corte.

Não há runtime paralelo nem feature flag de migração. Após a chamada de modelo,
um resume pode repetir custo se a confirmação não foi persistida; essa é uma
propriedade explícita da operação e não autoriza retry adicional no node.
