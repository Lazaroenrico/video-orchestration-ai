# Fonte única de verdade da topologia de nodes da pipeline

Data: `2026-08-25`

## Resultado

O inventário de nodes da pipeline deixou de estar triplicado. `orchestrator.graph.topology`
passa a ser a única declaração dos metadados canônicos (`NODE_SPECS` + estágios públicos),
e todas as visões consumidas por REST/SSE/progresso são derivadas dela:

- `web/server.py`: `PIPELINE_NODES`, `ITEM_UPDATE_NODES` e `NODE_LABELS` agora são
  importados de `graph.topology` (as cópias literais foram removidas);
- `orchestrator/progress.py`: `STAGES`, `NODE_STAGE`, `_ITEM_NODES` e `_TERMINAL_NODE`
  são derivados de `graph.topology` (`ITEM_PROGRESS_NODES`/`TERMINAL_NODE`);
- `graph/builder.py` valida os registros reais contra a tabela canônica em tempo de
  construção (`validate_registrations`), falhando rápido em qualquer divergência.

Adicionar um node novo agora exige editar apenas o registro no builder e um `NodeSpec`
na tabela `NODE_SPECS`. Os valores públicos (rótulos, ordem e chaves de payload de
API/SSE) permanecem idênticos — verificado byte a byte contra os literais anteriores.

## Mudanças de contrato

Nenhuma. A derivação é interna; payloads de API/SSE (labels, stage_ids, conjuntos de
nodes que emitem eventos) são idênticos aos anteriores. Compatibilidade confirmada por
script comparando cada visão derivada com os literais removidos (todos iguais).

## RED → GREEN

- **RED:** `tests/test_graph_topology.py` — as asserções de identidade
  (`progress.STAGES is topology.STAGES`, `server.NODE_LABELS is topology.NODE_LABELS`,
  etc.) falhavam porque `progress.py` e `web/server.py` ainda mantinham cópias próprias;
  `test_builder_registrations_are_covered_by_topology` falhava com
  `TopologyError: node 'voice_candidates' registrado no grafo mas ausente de
  graph/topology.py`; e o novo
  `test_validate_registrations_requires_only_configured_tiers` falhava porque configs
  com subconjunto dos tiers padrão eram rejeitadas indevidamente.
- **GREEN:** adicionado `NodeSpec("voice_candidates", "batch", None, None)` (node
  interno, sem estágio/rótulo públicos — comportamento idêntico ao anterior);
  `progress.py` e `web/server.py` passam a importar as visões de `graph.topology`;
  `validate_registrations` passa a exigir somente os tiers configurados
  (`required_item` = specs de item fora de `DEFAULT_TIERS` ∪ `set(tiers)`).
- **REFACTOR:** consolidação do bloco de imports em `progress.py` após `ruff --fix`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `TopologyError: node 'voice_candidates' ... ausente de graph/topology.py` ao construir o grafo (derrubava `test_web_endpoints` e `test_runtime_contract_resume`). | O builder registra `voice_candidates` (builder.py:251, cf. DECISIONS.md:748), mas a tabela canônica não tinha spec para ele — exatamente o tipo de drift que a validação deve capturar. | `NodeSpec("voice_candidates", "batch", None, None)` na ordem de registro (interno: não emite `node_start/node_end` nem vira estágio público, como antes). |
| `TopologyError: node 'kling'/'seedance'/'ltx' declarado como 'item' mas não registrado no subgrafo` em pipelines com menos tiers que o padrão (ex.: só `ltx_modified`). | `validate_registrations` exigia **todos** os specs de item de nível `item`, inclusive os tiers padrão não configurados pela config. | `required_item` agora exclui `DEFAULT_TIERS` dos specs obrigatórios e exige apenas `set(tiers)` configurados; tiers configurados ausentes continuam sendo erro. |
| `test_internal_batch_nodes_stay_silent_in_public_views` falhou esperando `feedback` entre os nodes totalmente silenciosos. | Expectativa errada do teste novo: `feedback` tem rótulo público ("Feedback") e apenas não vira estágio (`stage=None`). | Teste corrigido para refletir o comportamento desejado (silenciosos = `finalize_voices`, `voice_candidates`; `feedback` emite rótulo sem estágio). |

## Verificação final

- `rtk proxy python -m pytest <20 arquivos que importam progress/server/builder/topology>
  --no-cov -p no:cacheprovider` → **376 passed** (inclui `test_progress`,
  `test_web_endpoints`, `test_api_v2`, `test_web_item_updates`, `test_builder`,
  `test_creative_plan_graph`, `test_runtime_contract_resume`, etc.);
- `tests/test_graph_topology.py` isolado → **16 passed**;
- Re-run pós-formatação (`test_graph_topology`, `test_progress`, `test_web_endpoints`,
  `test_api_v2`, `test_web_item_updates`, `test_builder`) → **169 passed**;
- Script de compatibilidade comparando `PIPELINE_NODES`, `ITEM_UPDATE_NODES`,
  `NODE_LABELS`, `NODE_STAGE`, `_ITEM_NODES`, `_TERMINAL_NODE` e `STAGES` (conteúdo e
  ordem) contra os literais antigos → **todas idênticas**;
- `ruff check` + `ruff format --check` nos arquivos tocados → **All checks passed**;
- Testes dependentes de PostgreSQL não executados (limitação conhecida de infra local,
  sem servidor em 127.0.0.1:5432).

## Pendências ou bloqueios externos

Nenhum.
