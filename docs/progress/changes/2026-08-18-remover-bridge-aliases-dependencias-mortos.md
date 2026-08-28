# Remoção de bridge, aliases e dependências mortos

Data: `2026-08-18`

## Resultado

Remoção estrita dos adapters, bridge Node.js, arquivos de manifesto raiz (`package.json`, `package-lock.json`), testes e camadas de runtime do `Dockerfile` classificados comprovadamente como `dead` no inventário canônico da issue #8. Todos os itens classificados como `compatibility` permanecem suportados e operacionais pelo período de transição aprovado. Os três perfis de configuração (`config/`, `config-mock/`, `config-staging/`) carregam de forma íntegra e sem chamadas de rede.

## Mudanças de contrato

Nenhuma quebra de contrato público ativo nos perfis versionados.
- **Adapters e scripts removidos (`dead`):**
  - `TopazUpscaleAdapter` (`src/orchestrator/adapters/topaz_upscale.py`)
  - `ReplicateUpscaleAdapter` (`src/orchestrator/adapters/replicate_upscale.py`)
  - `VercelGatewayVideoAdapter` (`src/orchestrator/adapters/vercel_gateway_video.py`)
  - `VercelSeedanceAssemblyAdapter` (`src/orchestrator/adapters/vercel_seedance_assembly.py`)
  - `scripts/vercel_generate_video.mjs`
  - Testes unitários associados: `tests/test_vercel_gateway_video.py`, `tests/test_vercel_seedance_assembly.py`, `tests/test_replicate_upscale.py`.
- **Dependências e container (`dead`):**
  - `package.json` e `package-lock.json` na raiz do repositório (mantendo preservados e independentes `front/package.json` e `deploy/cloudflare/package.json`).
  - Instalação/cópia de Node LTS e `npm` no estágio `runtime` do `Dockerfile`.
- **Compatibilidade preservada (`compatibility`):**
  - `creator_real_vercel`, `creator_real_replicate`, `creator_vercel_replicate_voice`, `creator_real`, `ElevenLabsVoiceAdapter`, `ReplicateVoiceAdapter`, `anthropic`, `anthropic_sdk_gateway`.
- Decisões arquiteturais registradas em [`docs/DECISIONS.md`](../../DECISIONS.md).

## RED → GREEN

- **RED:** Execução de suíte de testes de caracterização (`tests/test_alias_classification.py`) falhando na asserção de ausência dos arquivos dead físicos (`test_dead_files_and_bridges_are_purged`), Dockerfile runtime sem Node (`test_dockerfile_runtime_does_not_install_node`) e ausência de adapters no registry (`test_dead_adapters_not_in_registry_map`).
- **GREEN:** Remoção dos arquivos mortos, remoção dos imports e entradas em `src/orchestrator/registry.py`, simplificação de `src/orchestrator/adapters/creator_real.py` (remoção de `topaz` no-op) e ajuste do `Dockerfile` eliminando o runtime Node do container final.
- **REFACTOR:** Limpeza de imports e referências legadas em testes (`tests/test_creator_real.py`, `tests/test_registry_composite.py`, `tests/test_replicate_throttle.py`, `tests/test_tracing_coverage.py`).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Erros de import nos testes `test_tracing_coverage.py` e `test_registry_composite.py` após remoção dos arquivos mortos | Módulos de teste ainda importavam diretamente classes `TopazUpscaleAdapter`, `VercelGatewayVideoAdapter`, `VercelSeedanceAssemblyAdapter` e `ReplicateUpscaleAdapter` | Atualização dos testes para validar apenas os adapters suportados e de compatibilidade ativos. |
| Testes em `test_creator_real.py` instanciando `RealCreatorAdapter` com parâmetro `topaz` | Parâmetro legado `topaz` era repassado nos fixtures de teste | Remoção do argumento `topaz` em todos os testes unitários de `test_creator_real.py`. |

## Verificação final

- `rtk proxy uv run pytest tests/test_alias_classification.py tests/test_creator_real.py tests/test_tracing_coverage.py tests/test_replicate_throttle.py tests/test_registry_composite.py tests/test_progress_docs.py --no-cov`: 115 passed.
- `rtk proxy uv run pytest --ignore-glob="*postgres*" --ignore-glob="*migration*" --no-cov`: 1228 passed, 2 skipped.
- Carga determinística dos 3 perfis (`config`, `config-mock`, `config-staging`) validada via `LanguageRuntime` e `orchestrator.config`.
- Verificação de ausência de referências órfãs (`grep_search` e `find` para scripts e pacotes mortos).
- `rtk proxy uv run pytest tests/test_progress_docs.py --no-cov`: 5 passed.

## Pendências ou bloqueios externos

Nenhum.
