# PROGRESS — handoff

Estado em **2026-06-29**. Suíte: **122 passando, 1 skip** (o teste `--live` do Judge,
opt-in, pulado sem `JUDGE_GATEWAY_URL`) + 1 warning benigno do LangGraph (ver falha #5).
Rodar: `rtk proxy python -m pytest`.

## Checklist de módulos (ordem TDD)

- [x] Scaffold (pyproject, uv venv, deps, configs) — `pyproject.toml`, `config/*.yaml`
- [x] `graph/state.py` — Item/BatchState/QCResult/JudgeVerdict + reducers (`test_state.py`)
- [x] `adapters/base.py` + `adapters/mock.py` — mocks determinísticos, custo por tier (`test_adapters_mock.py`)
- [x] `graph/routing.py` — tier routing + QC gate/loop (`test_routing.py`)
- [x] `nodes/stages.py` + `nodes/base.py` — os 10 stages como nodes
- [x] `registry.py` — provider→adapter (mock + replicate)
- [x] `graph/builder.py` — StateGraph (subgrafo per-item + fan-out via Send) (`test_builder.py`)
- [x] `graph/checkpoint.py` — SQLite async-compatible saver (`test_checkpoint.py`)
- [x] `runner.py` + `cli.py` — run/status/resume/list + relatório (`test_graph_e2e.py`, `test_cli.py`)
- [x] `adapters/judge.py` — gateway config-driven + cassette/replay + eval (`test_judge_eval.py`)
- [x] **Fase de subagentes (Opus coordena, Sonnet executa):**
  - [x] **A** `feedback_store.py` (Step 10) + `test_feedback_store.py` (13)
  - [x] **B** `adapters/replicate_video.py` (VideoPort, httpx async injetável) + `test_replicate_video.py` (11)
  - [x] **C** `tests/test_resume_partial.py` — resume parcial validado (ver falha #5)
- [x] **Loop de feedback fechado** — `runner`/`cli` com `--feedback-store`; `prior_winning_styles`
      injetado no ciclo seguinte; viés na geração de conceitos (`mock.generate_concepts(bias=...)`,
      `LLMPort.generate_concepts` atualizado). Testes: `test_feedback_loop.py` (2), `test_concept_bias.py` (4).
- [x] Docs — `CLAUDE.md`, `docs/DECISIONS.md`, este arquivo, `README.md`

## MVP — Vercel AI Gateway (D20) — em andamento

Decisão: usar o Vercel AI Gateway como ponto único para Claude e GPT Image 2.
Suíte atual: **132 passed, 1 skipped**. Nenhum teste muda nestas tasks.
Ordem de execução: Task 5 → 1 → 2 → 3 → 4 → 6.

- [ ] **Task 1** `adapters/openai_image.py` — adicionar `build_openai_image_vercel_adapter`
      (aponta para `https://ai-gateway.vercel.sh/openai/v1`, usa `AI_GATEWAY_API_KEY`)
- [ ] **Task 2** `adapters/creator_real.py` — adicionar `build_real_creator_vercel_adapter`
      (OpenAI via gateway + Topaz direto + ElevenLabs direto)
- [ ] **Task 3** `registry.py` — registrar `"creator_real_vercel"`
- [ ] **Task 4** `config/providers.yaml` — `llm: vercel_gateway_llm`, `creator: creator_real_vercel`,
      `video: replicate`
- [ ] **Task 5** `config/judge.yaml` — header Authorization aceitar `AI_GATEWAY_API_KEY`
- [ ] **Task 6** `.env.example` — marcar `TOPAZ_API_KEY` e `ELEVENLABS_API_KEY` como live

**Env vars para MVP:** `AI_GATEWAY_API_KEY`, `TOPAZ_API_KEY`, `ELEVENLABS_API_KEY`,
`REPLICATE_API_TOKEN`. Tabela completa em **D20**.

**Smoke test pós-implementação:**
```bash
# CI (sem chaves — deve passar 100%)
rtk proxy python -m pytest

# Instancia os adapters reais
AI_GATEWAY_API_KEY=<chave> TOPAZ_API_KEY=<chave> ELEVENLABS_API_KEY=<chave> REPLICATE_API_TOKEN=<chave> \
python -c "
from orchestrator.config import load_pipeline, load_providers
from orchestrator.registry import build_adapter_from_providers
p = load_pipeline(); prov = load_providers()
a = build_adapter_from_providers(prov, p)
print(type(a._by_role['llm']).__name__)      # AnthropicLLMAdapter
print(type(a._by_role['creator']).__name__)  # RealCreatorAdapter
print(type(a._by_role['video']).__name__)    # ReplicateVideoAdapter
"

# Run ponta a ponta
orchestrator run --batch 2 --offer "test product" --platform tiktok
```

## Próximos passos (v2, pós-MVP)

1. **Adapters reais** — *ligações criadas* (ver D17/D18); falta só chave no ambiente + flip:
   - [x] LLM via Vercel AI Gateway (`adapters/anthropic_llm.py`) — `llm: vercel_gateway_llm`
         + `AI_GATEWAY_API_KEY` ou `VERCEL_OIDC_TOKEN`.
   - [x] LLM direto Anthropic (`adapters/anthropic_llm.py`) — backward-compatible/legado;
         não é o caminho live recomendado do projeto.
   - [x] Creator: GPT Image 2 + Topaz + ElevenLabs (`adapters/creator_real.py`) — `creator: creator_real`
         + `OPENAI_API_KEY`/`TOPAZ_API_KEY`/`ELEVENLABS_API_KEY`.
   - [x] Vídeo Replicate (`adapters/replicate_video.py`, D14) — `video: replicate` + `REPLICATE_API_TOKEN`.
   - **Pendente p/ rodar real:** (a) expor as chaves no ambiente; (b) contratos HTTP de
     Topaz/ElevenLabs são assumidos (docstrings) — validar contra APIs reais com as chaves;
     (c) Steps 8/9 seguem mock (sem API única). Ver MVP acima (D20).
2. **Step 9 (distribuição) real** (cloud phones/proxies/scheduler) — hoje mock.
3. **Topologia data-driven**: mover nodes/edges para o `pipeline.yaml` (hoje fixa no builder).
4. **LangSmith**: setar `LANGSMITH_TRACING=true`/`LANGSMITH_API_KEY` p/ tracing; opcional
   subir o eval do Judge via `langsmith.evaluate` (hoje o evaluator roda local/offline).
5. [x] **CLI do loop**: `runner.run_cycles` + comando `orchestrator loop --cycles N
   --feedback-store ...` roda N ciclos encadeados; cada ciclo lê o feedback do anterior
   (viés nos conceitos) e grava o seu. Testes: `test_run_cycles.py` (3),
   `test_cli.py::test_cli_loop_*` (2). Ver **D16**.

## Falhas de teste investigadas (sintoma → causa raiz → correção)

1. **`process_item() missing 1 required positional argument: 'config'`**
   - Causa: o LangGraph só injeta `config` quando o parâmetro é tipado como
     `RunnableConfig`; estava `dict`.
   - Correção: anotar `config: RunnableConfig` no node (`graph/builder.py`).

2. **`SqliteSaver does not support async methods` (NotImplementedError)**
   - Causa: grafo roda via `ainvoke`, mas o checkpointer era o `SqliteSaver` sync.
   - Correção: usar `AsyncSqliteCompatSaver`, uma fachada async sobre `SqliteSaver`,
     porque `aiosqlite.connect` trava neste ambiente; ajustar os testes de checkpoint
     para a interface async (`aget_state`). (D9)

3. **`KeyError: '\n  "model"'` ao montar a request do Judge**
   - Causa: `str.format` interpretava as chaves literais do template JSON como campos.
   - Correção: substituir só os placeholders `{criteria_json}`/`{subject_json}` via
     `str.replace` (o template é JSON, não format-string).

4. **`Deserializing unregistered type ... Item` (warning, bloqueio futuro)**
   - Causa: pydantic Items no checkpoint sem tipo registrado no serializador.
   - Correção: `JsonPlusSerializer(allowed_msgpack_modules=[...Item/Artifact/QCResult])`. (D9)

5. **`RuntimeWarning: coroutine 'arun_with_retry' was never awaited`** (em `test_resume_partial.py`)
   - Sintoma: warning ao interromper um batch no meio (subagente C).
   - Causa: comportamento INTERNO do LangGraph — ao propagar a exceção, o executor
     (`pregel/_executor.py:181`) cancela as tasks pendentes do superstep do fan-out; as
     corrotinas pendentes são coletadas sem await.
   - Conclusão: **não é bug do produto**. Verificado que o resume parcial funciona correto
     no LangGraph 1.2.6 (checkpoint granular por task: itens concluídos não re-executam,
     pendentes sim; sem duplicar/perder). Warning é benigno; não foi suprimido para não
     mascarar comportamento real.
