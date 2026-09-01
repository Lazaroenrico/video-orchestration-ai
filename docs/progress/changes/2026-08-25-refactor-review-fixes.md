# Correções pós-revisão da refatoração estrutural

Data: `2026-08-25`

## Resultado

A refatoração ampla foi preservada e suas divergências observáveis foram fechadas:
tiers de vídeo configurados passam por uma única topologia runtime em builder,
runner, progresso, SSE, `item_update` e `/api/state`; `RunRegistry.pop` recuperou a
semântica de `dict`; a inferência de gênero deixou de casar substrings dentro de
outras palavras; e o backend R2 não reutiliza workers incompatíveis entre event
loops nem libera capacidade enquanto um worker cancelado ainda executa.

O guard de camadas de `ArtifactRecord` agora prova round-trip real no SQLite. As
fronteiras arquiteturais consolidadas estão registradas em
[`ADR-D52`](../../ADR-D52-refactor-foundations.md).

## Mudanças de contrato

- `topology_for_tiers()` resolve `PipelineTopology` para os tiers efetivos. Tiers
  customizados recebem stage `talking_head`, rótulo determinístico, progresso e
  snapshots incrementais; aliases públicos continuam apontando para a topologia
  default retrocompatível.
- `ProgressEventTranslator`, `build_progress`, `build_activity` e `_build_item_update`
  aceitam uma topologia opcional; runner e web injetam a visão da pipeline efetiva.
- `RunRegistry.pop(chave)` volta a levantar `KeyError`; somente o default explícito
  transforma ausência em valor de retorno.
- `infer_gender` considera palavras Unicode completas. Tokens explícitos e a
  precedência feminina foram preservados; `other`, `sherpa`, `elegante` e `elemento`
  agora são neutros.
- Operações boto3 do R2 mantêm limite de oito workers por loop. Cancelar o await não
  abandona o trabalho síncrono nem libera o slot antes de sua conclusão.
- `PoolTimeout` não exigiu mudança: na versão instalada ele herda de
  `psycopg.OperationalError`, já coberto por `DATABASE_UNAVAILABLE_ERRORS`.

## RED → GREEN

- **RED:** tier `pruna` era aceito pelo builder, mas não tinha label, stage,
  `item_update` ou progresso; testes falharam primeiro por ausência de
  `topology_for_tiers`, depois por argumento `topology` inexistente e projeções web
  que permaneciam `pending`.
- **GREEN:** `PipelineTopology` runtime, validação do builder e injeção pelos
  composition roots de runner/web tornaram `pruna` observável em todos os read
  models sem mudar a topologia default.
- **RED:** `RunRegistry.pop("ausente")` retornava `None` em vez de levantar
  `KeyError`; quatro casos de texto casavam `her`/`ele` como substrings.
- **GREEN:** sentinela distingue default omitido; tokenização Unicode por palavras
  completas substitui busca por substring.
- **RED:** dois usos do storage R2 em loops separados bloqueavam o segundo future.
  Depois da primeira correção, o teste de cancelamento mostrou que um segundo upload
  ultrapassava o limite enquanto o primeiro worker ainda estava bloqueado.
- **GREEN:** cada chamada usa worker curto sob semáforo loop-aware; `shield` mantém o
  future do worker vivo e uma cleanup task retém o slot até a conclusão real.
- **REFACTOR:** aliases default foram mantidos nas fachadas de topologia/progresso;
  o teste antes tautológico de `ArtifactRecord` foi trocado por persistência e leitura
  observáveis no repositório público.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Comando inicial de teste falhou com `python: command not found`. | O virtualenv não está no `PATH` deste sandbox. | Todos os testes passaram a usar `.venv/bin/python`. |
| Revisão estática apontou perda de fallback para `PoolTimeout`. | `PoolTimeout` é subclasse de `OperationalError` nesta versão; o tuple existente já o captura. | Nenhuma alteração redundante; dois testes de SSE/readiness e a hierarquia da exceção foram verificados. |
| Testes de `_execute_run` falharam com `KeyError: 'tiers'`. | Fixtures antigas simulavam uma pipeline parcial, mas a topologia runtime agora consome o contrato efetivo obrigatório. | Fixtures receberam um tier mínimo válido; as asserções de gate/catalog permaneceram intactas. |
| `ruff format --check` revelou a expressão solta `"PipelineTopology"` no fim de `topology.py`. | Export foi inserido fora de `__all__` durante a preservação dos aliases. | Símbolo movido para `__all__`; fontes de topologia/progresso/runner formatadas. |
| Suíte R2 parava entre testes ou após duas chamadas no mesmo loop. | O `ThreadPoolExecutor` global reutilizava um worker cuja fila não despertava no loop seguinte neste runtime. | Worker curto por chamada, com semáforo por loop e regressão explícita de reutilização. |
| Cancelar upload permitia que o próximo começasse antes do boto3 terminar. | `finally` liberava o semáforo embora a thread síncrona continuasse executando. | Future protegido e cleanup independente só liberam o slot após `concurrent_future.done()`. |
| Integração isolada de resume com creator real ficava no `select()` após o trabalho terminar e mudava imediatamente para `PASSED` quando o processo recebia sinal. | Neste sandbox, o wake-up `call_soon_threadsafe` do executor usado internamente pelo LangGraph não desperta o selector em alguns caminhos; as threads estavam ociosas e nenhuma asserção falhou. | Nenhuma mudança insegura no LangGraph; o recorte alterado foi validado separadamente e o comportamento foi registrado como limitação do runtime local. |
| Suíte completa parou ao chegar à primeira fixture PostgreSQL. | Não há servidor em `127.0.0.1:5432` neste sandbox. | Nenhuma; asserções e fixtures PostgreSQL foram preservadas. |

## Verificação final

- Bateria final consolidada do recorte ampliado: **426 passed**.
- Bateria integrada do recorte (topologia, runner, web, common, layering, runtime
  contract e config overlay): **372 passed**.
- Regressões R2, incluindo loops distintos e cancelamento: **23 passed**.
- Teste isolado da fixture de agent catalog corrigida: **1 passed**.
- Testes reais do fallback `PoolTimeout` em SSE/readiness: **2 passed**.
- `ruff check` nos módulos e testes alterados: **All checks passed**.
- `ruff format --check` nos dez módulos de produção alterados: **10 files already
  formatted**.
- Revisão independente de código executada após as correções: **APPROVE**, sem
  defeitos materiais no recorte final.
- Suíte completa com `-x`: avançou sem falha de código até a primeira fixture
  PostgreSQL e parou com `psycopg.OperationalError` por ausência do servidor local.

## Pendências ou bloqueios externos

- Executar as suítes PostgreSQL quando houver servidor acessível em
  `127.0.0.1:5432`, conforme a limitação de infraestrutura já documentada em
  `AGENTS.md`.
- Revalidar a suíte offline completa em um host onde callbacks de
  `ThreadPoolExecutor` despertem normalmente o selector do asyncio; testes do
  recorte e casos isolados terminam verdes neste sandbox.
