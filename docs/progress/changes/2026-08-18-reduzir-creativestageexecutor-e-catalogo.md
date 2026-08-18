# Reduzir CreativeStageExecutor e catálogo aos três stages criativos

Data: `2026-08-18`

## Resultado

Redução do `AgentCatalog` e do `StageExecutor` (`CreativeStageExecutor`) para suportar estritamente os três stages criativos delimitados (`concepts`, `scripts`, `creator_profiles`). Cada stage criativo agora aponta para um único `materializer`. Os stages determinísticos de mídia, QC, assembly, upscale e locução chamam suas respectivas funções/adapters diretamente sem passar por execução de agent/LangChain tools ou `execute_stage_tool`. Metadados mortos sem consumidor (`target_agent`, `function_path`, `capabilities`) foram removidos do runtime.

## Mudanças de contrato

- **`StageExecutionSpec`**:
  - Campo canônico `materializer: str` substitui a lista de ferramentas nos arquivos de configuração YAML (`config/agents.yaml`, `config-mock/agents.yaml`, `config-staging/agents.yaml`).
  - Compatibilidade transitória mantida via propriedade `@property def tools` e normalização na carga de `agents.yaml` com `tools: [...]`.
  - Remoção de metadados não utilizados em runtime: `target_agent` e `function_path`.
- **`AgentCatalog`**:
  - Catálogo padrão e serialização contêm exclusivamente os 3 estágios criativos (`concepts`, `scripts`, `creator_profiles`).
  - Estágios legados não criativos com `executor: tool` em arquivos legados são ignorados silenciosamente durante a janela de transição; se declarados com `executor: agent`, levantam erro com mensagem explicativa.
- **`TOOL_REGISTRY`**:
  - Reduzido aos 3 materializadores criativos (`generate_concepts`, `write_script`, `design_creator_roster`).
  - Removido `resolve_tool_function` e `ToolSpec.function_path` em favor de chamadas de funções diretas pelos nós da pipeline.
- **Nodes de Pipeline (`src/orchestrator/nodes/stages.py`)**:
  - `node_roster`, `node_voice_candidates`, `node_finalize_voices`, `make_gen_node`, `node_product_demo`, `node_qc`, `node_voiceover`, `node_assemble`, `node_upscale` chamam diretamente suas funções e adapters em vez de usar `execute_stage_tool`.

## RED → GREEN

- **RED:**
  - `test_agent_catalog.py`, `test_tools.py`, `test_stages_coverage.py` e `test_live_config_no_mock.py` falharam quando os estágios não criativos foram removidos de `TOOL_REGISTRY` e `AgentCatalog`, quando `target_agent` foi removido de `as_dict()`, e quando `execute_stage_tool` foi restrito a `is_agent_stage_allowed`.
- **GREEN:**
  - Implementado `materializer` em `StageExecutionSpec` e atualizado `build_agent_catalog` com suporte a `materializer` e compatibilidade transitória com `tools: [...]`.
  - `execute_stage_tool` atualizado para validar `is_agent_stage_allowed` e verificar correspondência com `spec.materializer` ou `spec.tools`.
  - Nós de mídia, QC, assembly, voiceover e upscale refatorados para chamadas diretas de ferramentas e adapters.
  - Testes e configs atualizados com assertions canônicas para o catálogo mínimo e materializadores.
- **REFACTOR:**
  - Removidos imports não utilizados e adicionados testes de deleção explícitos para garantir ausência de `function_path` e metadados mortos.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `KeyError: 'video'` em `test_live_config_activates_agent_mode_only_on_creative_stages` | Teste de configuração live esperava que `video`, `roster`, `qc`, etc., estivessem presentes no `AgentCatalog` em modo tool. | Atualizado teste para validar que apenas os 3 stages criativos residem no catálogo e stages não criativos levantam `KeyError`. |
| `AttributeError: 'object' object has no attribute 'design_voice_candidates'` em testes de voz em `test_stages_coverage.py` | Testes monkeypatchavam `execute_stage_tool` que foi substituído por chamadas diretas a `derive_creator_voice_spec_tool` e `design_creator_voice_tool`. | Atualizados monkeypatches nos testes para apontar para as tools diretas correspondentes. |
| `Failed: DID NOT RAISE ValueError` para stage inválido `'nope'` em `test_agent_catalog.py` | `build_agent_catalog` ignorava qualquer stage ausente que tivesse `executor != 'agent'` em vez de restringir a tolerância aos estágios legados conhecidos. | Adicionado `_LEGACY_NON_CREATIVE_STAGES` garantindo que nomes totalmente desconhecidos como `'nope'` levantem `ValueError` explicativo. |
| `NameError: name 'pytest' is not defined` em `test_live_config_no_mock.py` | `pytest.raises` foi utilizado sem importar `pytest` no topo do módulo de teste. | Adicionado `import pytest` no topo de `tests/test_live_config_no_mock.py`. |
| `ModuleNotFoundError: No module named 'tests'` em `test_system_prompt.py` | Import `from tests.conftest import TIERS` falhou durante a coleta do pytest com `testpaths = tests`. | Adicionado fallback `try: from conftest import TIERS except ImportError: from tests.conftest import TIERS`. |

## Verificação final

- `uv run pytest -o addopts="" tests/test_agent_catalog.py tests/test_stage_executor.py tests/test_tools.py tests/test_runtime_contract.py` -> 85 passed.
- `uv run pytest -o addopts="" tests/test_stages_coverage.py tests/test_video_agent_node.py tests/test_builder.py tests/test_graph_e2e.py tests/test_staging_contract.py tests/test_live_config_no_mock.py` -> 113 passed.
- `uv run pytest -o addopts="" tests/test_web_spa.py` -> 30 passed.
- `uv run pytest -o addopts="" --ignore-glob="*postgres*" --ignore-glob="*storage_migration*"` -> 1264 passed.
- `uv run ruff check src tests` -> All checks passed!

## Pendências ou bloqueios externos

Nenhum.
