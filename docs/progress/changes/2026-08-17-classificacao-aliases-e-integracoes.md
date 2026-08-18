# Classificação de aliases, adapters e integrações legadas

Data: `2026-08-17`

## Resultado

Classificação formal e inventário completo de todos os aliases, adapters, bridges e dependências do motor de orquestração nas categorias `supported`, `compatibility` ou `dead`, com identificação de evidências de uso, janelas de depreciação e estratégias de rollback para subsidiar a issue #16. Nenhum código de produção foi removido nesta etapa (read-only characterization).

## Mudanças de contrato

Nenhuma. As interfaces públicas e adapters registrados em `src/orchestrator/registry.py` e `src/orchestrator/language_runtime.py` foram mantidos intactos.

## Inventário e Matriz de Classificação

### 1. Papel Creator e Voz
- **`creator_vercel_elevenlabs_design`** (`supported`): Provider ativo em `config/providers.yaml`. Compõe `OpenAIImageAdapter` (via Vercel AI Gateway) com `ElevenLabsVoiceDesignAdapter` para geração de candidatos, previews e finalização de voz.
- **`creator_real_vercel`** (`compatibility`): Alias registrado em `registry.py` apontando para `build_real_creator_vercel_adapter`. Janela: manter até remoção no escopo da issue #16.
- **`creator_real_replicate` / `creator_vercel_replicate_voice`** (`compatibility`): Compõe OpenAI Image com voz ElevenLabs hospedada no Replicate (`ReplicateVoiceAdapter`). Janela: compatibilidade até #16.
- **`creator_real`** (`compatibility`): Adapter direto com chaves de ambiente `OPENAI_API_KEY` e `ELEVENLABS_API_KEY`. Janela: compatibilidade até #16.
- **`ElevenLabsVoiceAdapter`** (`compatibility`): Adapter legado de voz direta/estática (`adapters/elevenlabs_voice.py`). Janela: compatibilidade para modo `legacy` até #12/#16.
- **`ReplicateVoiceAdapter`** (`compatibility`): Adapter de voz Replicate (`adapters/replicate_voice.py`). Janela: compatibilidade até #16.
- **`TopazUpscaleAdapter`** (`dead`): Upscaler de imagem de face (`adapters/topaz_upscale.py`). Não registrado em `_ADAPTERS`; parâmetro em `creator_real.py` é no-op ignorado. Aprovado para remoção na issue #16.

### 2. Papel Vídeo
- **`replicate` (`ReplicateVideoAdapter`)** (`supported`): Provider ativo em `config/providers.yaml`. Executa P-Video/LTX e LatentSync 2-estágios com ledger de efeitos duráveis e reconciliação.
- **`mock` (`MockAdapter`)** (`supported`): Provider ativo em `config-mock/` e `config-staging/`. Determinístico e zero-cost.
- **`vercel_gateway_video` (`VercelGatewayVideoAdapter`)** (`dead`): Adapter experimental (`adapters/vercel_gateway_video.py`) que invoca bridge Node (`scripts/vercel_generate_video.mjs`). Sem uso nos perfis versionados. Aprovado para remoção na issue #16.

### 3. Papel Montagem (Assembly)
- **`ffmpeg_assembly` (`FFmpegAssemblyAdapter`)** (`supported`): Provider ativo em `config/providers.yaml`. Concatenação de clips e mux de áudio local via FFmpeg.
- **`mock` (`MockAdapter`)** (`supported`): Provider ativo em `config-mock/` e `config-staging/`.
- **`vercel_seedance_assembly` (`VercelSeedanceAssemblyAdapter`)** (`dead`): Adapter experimental (`adapters/vercel_seedance_assembly.py`) que invoca bridge Node para Seedance 2.0. Sem uso nos perfis versionados. Aprovado para remoção na issue #16.

### 4. Papel Upscale
- **`passthrough_upscale` (`PassthroughUpscaleAdapter`)** (`supported`): Provider ativo em `config/providers.yaml`. Pass-through no-op pós-montagem.
- **`replicate_upscale` (`ReplicateUpscaleAdapter`)** (`dead`): Adapter de upscale de imagem (`adapters/replicate_upscale.py`). Não registrado em `_ADAPTERS` e sem chamadas no grafo. Aprovado para remoção na issue #16.

### 5. Runtime de Linguagem e Deployments LLM
- **`mock` (`MockChatModel`)** (`supported`): Runtime LLM offline determinístico.
- **`vercel_gateway_llm`** (`supported`): Provider ativo em `config/providers.yaml` via `ChatOpenAI` no Vercel AI Gateway (`AI_GATEWAY_BASE_URL`).
- **`anthropic_sdk_gateway`** (`compatibility`): Deployment via `ChatAnthropic` no endpoint Anthropic do gateway. Centralização prevista na issue #10.
- **`anthropic`** (`compatibility`): Deployment direto via `ChatAnthropic` com `ANTHROPIC_API_KEY`. Centralização prevista na issue #10.

### 6. Node Bridge e Dependências
- **`scripts/vercel_generate_video.mjs`** (`dead`): Script bridge invocado unicamente pelos adapters dead `vercel_gateway_video` e `vercel_seedance_assembly`. Aprovado para remoção na issue #16.
- **Root `package.json`, `package-lock.json`, `node_modules`** (`dead`): Dependências exclusivas do bridge (`"ai": "^6.0.0"`). O frontend React vive em `front/` e é independente (`supported`). Aprovados para remoção na issue #16.
- **Instalação do Node no `Dockerfile`** (`dead`): Camada de instalação do runtime Node.js no Dockerfile da pipeline Python. Aprovada para remoção na issue #16.

### 7. Evaluation e Protocolos
- **`JudgePort`** (`dead` em produção): Protocolo de adapter removido de `adapters/base.py` e isolado exclusivamente em `orchestrator.evaluation.judge` (`supported` para testes e benchmarks).

## RED → GREEN

- **RED:** Ausência de documento canônico e caracterização formal de inventário, classificação e plano de depreciação/rollback para a issue #16.
- **GREEN:** Mapeamento completo dos 19 itens/adapters/bridges e validação de não-regressão de carga dos 3 perfis (`config/`, `config-mock/`, `config-staging/`) e suíte de testes.
- **REFACTOR:** Não aplicável (sem alterações no código executável).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Dúvida sobre dependência do Node no `Dockerfile` | O `Dockerfile` continha `curl -fsSL https://deb.nodesource.com/...` | Constatado que o runtime Node era exigido unicamente pelo script `vercel_generate_video.mjs` dos adapters dead `vercel_gateway_video` e `vercel_seedance_assembly`. Classificado como dead para remoção na #16. |

## Verificação final

- `PYTHONPATH=. rtk proxy uv run pytest tests/test_progress_docs.py --no-cov`: 5 passed (validação de formato, links e manifesto).
- `PYTHONPATH=. rtk proxy uv run pytest tests/test_registry_composite.py tests/test_creator_real.py tests/test_language_runtime.py tests/test_runtime_contract.py tests/test_replicate_video.py tests/test_ffmpeg_assembly.py tests/test_integrity_qc.py --no-cov`: 130 passed.
- Verificação de carga sem rede dos 3 perfis (`config/`, `config-mock/`, `config-staging/`).

## Pendências ou bloqueios externos

- Aprovação humana da classificação (HITL) para autorizar a execução das issues downstream (#10, #14, #15, #16).
