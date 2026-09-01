# ADR-D47: contratos de prompt e fronteira trusted/untrusted dos agents

Data: `2026-08-14`  
Status: aceito

## Decisão

Como complemento a D46, cada execução criativa envia o prompt estático do catálogo
sem dados de campanha. O runtime acrescenta uma mensagem separada
`SERVER_EXECUTION_CONSTRAINTS`, composta por allowlist de controles server-owned
(quantidade de conceitos, limites de roteiro, dois creators e concept IDs), e uma
mensagem `UNTRUSTED_STAGE_DATA` com o conteúdo da campanha. Campos desconhecidos não
podem atravessar a fronteira confiável; conteúdo do usuário não entra em system prompt,
traces, API ou logs.

O terminal continua `create_agent(tools=[], response_format=ToolStrategy(...))`.
Os três outputs usam `creative-v2`; creator profiles reutiliza o contrato canônico de
dois creators e índices 0/1. O output agentic de scripts exige a primeira spoken beat
como `hook`, sem alterar o parser legado `script_result_from_text`. Evidência de
performance exige snapshot de performance; `provided_fact` e `cold_test` permanecem
válidos sem ele.

Os prompts operacionais são versionados como `concepts-v3`, `scripts-v3` e
`creators-v3`, byte a byte iguais entre os três perfis. Eles descrevem uma única
resposta estruturada, sem instruções de chamada de domínio ou promessas de retry.

## Consequência

Controles de execução continuam auditáveis e independentes do texto criativo, enquanto
o modelo recebe contexto de campanha sem autoridade para alterar contagens, IDs,
limites ou roteamento. A recuperação estrutural continua limitada pelo middleware do
ToolStrategy, sem reintroduzir loop de ferramenta.

Detalhes do runtime LangChain permanecem em [`ADR-D46`](ADR-D46-langchain-native-language-runtime.md).
