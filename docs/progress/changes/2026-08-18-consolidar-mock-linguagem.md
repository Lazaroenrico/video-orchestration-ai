# Consolidação da implementação mock de linguagem no LanguageRuntime

Data: `2026-08-18`

## Resultado

Consolidação completa de toda a responsabilidade de geração e processamento mock de linguagem em `LanguageRuntime` (`src/orchestrator/language_runtime.py`). Os métodos legados de linguagem (`generate_concepts`, `write_script`) e seus helpers determinísticos (`_HOOK_STYLES`, `_unit`, `_terminal_submission`) foram purgados de `MockAdapter` (`src/orchestrator/adapters/mock.py`). As ferramentas criativas (`generate_concepts_tool` e `write_script_tool`) consomem exclusivamente `ctx.language_runtime`, sem fallback para `ctx.adapter`. Os adapters de domínio (`MockAdapter`, `CompositeAdapter`) permanecem estritamente focados em mídia e efeitos (`creator`, `video`, `qc`, `assembly`, `upscale`).

## Mudanças de contrato

- `MockAdapter` não implementa mais `generate_concepts` nem `write_script`.
- `LanguageRuntime` centraliza a geração estruturada (`_mock_structured_submission`), direta (`generate_concepts`, `write_script`) e unifica as sementes determinísticas baseadas em SHA-256 e bias.
- `generate_concepts_tool` e `write_script_tool` exigem `ctx.language_runtime` em `ToolContext` e falham com `RuntimeError` caso esteja ausente, sem fallback silencioso para `ctx.adapter`.
- `tool_context_from_config` padroniza o provisionamento de `LanguageRuntime.from_provider("mock", ...)` quando `language_runtime` não for injetado explicitamente no dicionário `configurable`.
- Nenhuma alteração nos contratos públicos dos nós do grafo LangGraph ou na API V2.

## RED → GREEN

- **RED:**
  - `test_mock_adapter_has_no_language_methods` em `tests/test_adapters_mock.py` falhou pois `MockAdapter` ainda possuía `generate_concepts` e `write_script`.
  - `test_creative_tools_fail_fast_without_language_runtime_in_legacy_mode` em `tests/test_creative_agent_tools.py` falhou pois as tools utilizavam fallback para `ctx.adapter`.
  - `tests/test_language_runtime.py` cobriu novos contratos de `generate_concepts`, `write_script` e `generate_structured`.
- **GREEN:**
  - Remoção de `generate_concepts`, `write_script`, `_terminal_submission` e `_HOOK_STYLES` de `src/orchestrator/adapters/mock.py`.
  - Implementação unificada dos geradores determinísticos em `src/orchestrator/language_runtime.py`.
  - Remoção do fallback para `ctx.adapter` em `src/orchestrator/tools/concepts.py` e `src/orchestrator/tools/scripts.py`.
  - Atualização dos testes unitários e fixtures para usar `LanguageRuntime` em chamadas criativas.
- **REFACTOR:**
  - Garantia de 100% de cobertura nos módulos `tools/concepts.py`, `tools/scripts.py`, `tools/creator_profiles.py`, `tools/qc.py`, `tools/registry.py` e `adapters/mock.py`.
  - Ajuste de `_config` e spies em `tests/test_tools.py` com `_SpyLanguageRuntime`.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `ImportError: No module named 'tests'` em `tests/test_system_prompt.py` | Configuração de pytest no `pyproject.toml` não continha `pythonpath = [".", "src"]`. | Adicionado `pythonpath = [".", "src"]` nas opções de pytest do `pyproject.toml`. |
| `RuntimeError: generate_concepts requires LanguageRuntime in ToolContext` em `tests/test_builder.py` | Fixture `run_config` em `tests/conftest.py` não passava `language_runtime` em `configurable`. | Atualizado `run_config` para injetar `LanguageRuntime.from_provider("mock", pipeline_cfg)` e adicionado fallback de segurança em `tool_context_from_config`. |
| `ToolOutputError` em `tests/test_tools.py` | `_SpyAdapter` era usado para simular retorno de `generate_concepts` e `write_script`. | Criada a classe `_SpyLanguageRuntime` e injetada no `ToolContext` nos testes de validação de saída de linguagem. |

## Verificação final

- `pytest tests/test_adapters_mock.py tests/test_language_runtime.py tests/test_creative_agent_tools.py tests/test_concept_bias.py tests/test_small_gaps.py tests/test_tools.py tests/test_stages_coverage.py`: 126 testes aprovados, cobertura 100% nos módulos de tools e mock adapter.
- `pytest` suíte offline completa: 1243 testes unitários e de integração aprovados, 0 falhas.
- `ruff check src tests`: 0 erros de linting.

## Pendências ou bloqueios externos

Nenhum.
