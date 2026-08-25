# Config base + overlay (config-base/) e resolução de prompts perfil→base

Data: `2026-08-25`

## Resultado

Os três perfis (`config/`, `config-mock/`, `config-staging/`) não duplicam mais
conteúdo: o que era idêntico entre eles migrou para `config-base/` (pipeline.yaml,
agents.yaml, judge.yaml, providers.yaml vazio-de-propósito e os 6 prompts de
`prompts/agents/`). O loader (`src/orchestrator/config.py`) agora faz deep-merge
base + perfil (overlay vence por chave; listas são substituídas wholesale) e aplica
expansão `${VAR}`/`${VAR:-default}` a TODOS os YAMLs (antes, só judge.yaml). Prompts
resolvem primeiro no perfil e caem para a base (`agent_catalog.py` ganhou
`fallback_dirs`, retrocompatível). A configuração efetiva dos três perfis é
**byte-idêntica à de antes do refactor** — zero drift comprovado por snapshot.

## Mudanças de contrato

- Nenhuma mudança na superfície pública: `load_pipeline/load_providers/load_judge/
  load_agent_catalog` mantêm assinaturas e semântica; `build_agent_catalog` ganhou
  parâmetro opcional `fallback_dirs: tuple[Path, ...] = ()`.
- Novo diretório `config-base/` é agora requisito de deploy: o Dockerfile passou a
  copiá-lo (`COPY config-base/ ./config-base/`). Perfis continuam exigindo os 4 YAMLs
  (stubs de comentários onde tudo vem da base) — validação do `scripts/dev-local`
  segue válida.
- Caching de YAML parseado: **não implementado**, por decisão de segurança (evitar
  bugs de config obsoleta entre chamadas no mesmo processo).
- Documento canônico do layout: seções Layout de `AGENTS.md` e `CLAUDE.md`
  (atualizadas de forma idêntica).

## RED → GREEN

- **RED:** `tests/test_config_overlay.py` novo — deep-merge (dict funde, lista
  substitui, chave só-da-base preservada), expansão de env em pipeline/providers/
  judge/agents, fallback de prompt perfil→base (e erro alto quando ausente nos dois),
  rede anti-drift (prompts compartilhados idênticos ou ausentes do perfil) e hash de
  prompt igual entre os três perfis. 7 falhas confirmadas antes da implementação.
- **GREEN:** `_deep_merge` + `_load_merged_yaml` + `config_base_dir()` em
  `config.py`; expansão de env aplicada em todos os YAMLs carregados; `fallback_dirs`
  em `_load_system_prompt`/`build_agent_catalog`; criação de `config-base/`;
  eslimamento dos perfis (ver abaixo).
- **REFACTOR:** não aplicável além do próprio eslimamento (parte do GREEN).

Conteúdo movido verbatim:

| Arquivo | Origem | Destino |
| --- | --- | --- |
| judge.yaml | config/ (= mock = staging, md5 igual) | `config-base/judge.yaml`; stubs nos 3 perfis |
| agents.yaml (9 stages tool) | config-mock/ (= staging) | `config-base/agents.yaml`; stubs em mock/staging |
| agents.yaml (3 stages agent) | config/ | permanece em `config/agents.yaml` como overlay |
| pipeline.yaml (batch/qc/clip comum/assembly comum/roster/voice comum) | interseção dos 3 | `config-base/pipeline.yaml` |
| tiers, fps/draft/timeout_ms, latentsync, video, qc.required_clip_count, assembly extra, agent budget, voice mode/provider/retry/costs | cada perfil | permanecem no pipeline.yaml do perfil |
| concepts/scripts/creators/_shared .md | config/ (idênticos nos 3) | `config-base/prompts/agents/`; removidos dos perfis |
| persona.md e video.md | variante mock/staging | `config-base/prompts/agents/`; `config/` mantém as variantes live como override |

`providers.yaml` ficou integralmente por perfil (nenhum valor é compartilhado);
`config-base/providers.yaml` existe só como comentário documentando isso (merge sobre
documento vazio é no-op).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `test_langchain_agent_contracts.py::test_agent_prompts_have_v3_contracts_and_profiles_are_byte_identical` leria `prompts/agents/*` direto do perfil (FileNotFoundError após mover os prompts). | O teste codificava a localização antiga dos prompts, não um contrato de conteúdo. | Atualizado para ler as fontes compartilhadas em `config-base/` e comparar `prompt_hash` dos três stages criativos contra o perfil live — mesma força de asserção (bytes idênticos ⇒ mesmo hash), sem afrouxar nada. Conteúdo dos prompts preservado byte a byte (md5 conferido na cópia). |
| `test_runtime_contract.py`: 3 chamadas `build_agent_catalog(..., base_dir="config-mock")` com `system_prompt_path` real falhariam com "not found". | Os prompts saíram do diretório do perfil. | Essas 3 chamadas apontam agora para `base_dir="config-base"` (mesmos bytes em disco); as outras 4 que não leem prompt ficaram intactas. |
| 13 ERRORs em `tests/test_legacy_import.py` (fixture `postgresql`). | Sem servidor PostgreSQL em 127.0.0.1:5432 neste sandbox — limitação de infraestrutura local pré-existente, não relacionada ao refactor. | Nenhuma (fora de escopo; testes PostgreSQL intocados). |

## Verificação final

- Equivalência (método): script `/tmp/snapshot_config.py` despeja via API pública —
  `load_pipeline`, `load_providers`, `load_judge` e `load_agent_catalog().as_dict()`
  (inclui `prompt_hash`, que verifica transitivamente os bytes compostos
  `_shared+stage`) — para os três perfis, com env controlada
  (`env -u AI_GATEWAY_API_KEY -u JUDGE_GATEWAY_URL -u JUDGE_GATEWAY_KEY`).
  Resultado: `diff /tmp/pre_refactor.json /tmp/post_refactor.json` → **vazio**
  (793 linhas idênticas; snapshots tirados antes e depois do refactor).
- `rtk proxy python -m pytest tests/test_config_overlay.py tests/test_live_config_no_mock.py tests/test_registry_composite.py tests/test_voice_factory.py tests/test_langchain_agent_contracts.py tests/test_runtime_contract.py tests/test_agent_prompt_security.py tests/test_agent_catalog.py tests/test_scope_eval.py tests/test_judge_eval.py tests/test_staging_contract.py tests/test_storage_factory.py --no-cov -p no:cacheprovider` → **163 passed, 2 skipped** (skips legítimos opt-in `--live`).
- Testes que leem os perfis por caminhos mais pesados: `tests/test_operations.py`,
  `tests/test_dev_local.py`, `tests/test_legacy_import.py` (parte não-PostgreSQL),
  `tests/test_web_endpoints.py` + `tests/test_web_item_updates.py` → **27 passed** +
  **108 passed** (erros apenas na fixture PostgreSQL já explicada acima).
- `rtk proxy ruff check src/orchestrator/config.py src/orchestrator/agent_catalog.py
  tests/test_config_overlay.py tests/test_langchain_agent_contracts.py
  tests/test_runtime_contract.py` → **All checks passed!**
- `tests/test_progress_docs.py` confere checksums apenas de
  `docs/progress/archive/` — não cobre configs/prompts; permanece verde sem ajustes.

## Pendências ou bloqueios externos

- Deploy Cloudflare (`deploy/cloudflare/src/index.ts` usa `/app/config-staging`)
  depende da imagem nova com `COPY config-base/` — coberto pelo Dockerfile atualizado;
  nenhum outro arquivo de deploy precisou mudar.
- Referências históricas a `config*/` em `docs/` (ADRs, PLANs, DEMO, diagramas)
  continuam válidas pois os nomes de diretório não mudaram; nenhuma foi alterada.
