# UGC Orchestrator

Motor de orquestração para a pipeline de **AI UGC em escala** (500+ vídeos/semana)
descrita em `Context.md`. O motor é construído via **TDD** sobre **LangGraph /
LangChain / LangSmith** e permite misturar adapters reais e mock por papel.

- `config-mock/` roda dry-run determinístico, sem chamadas externas e custo zero.
- `config/` é o perfil live/híbrido atual: LLM + creator + vídeo LTX 2.3 Fast via
  APIs reais; QC, assembly e distribuição continuam mock.

## Pipeline (10 passos)

1. Conceitos (Claude) · 2. Scripts (Claude) · 3. Creator reutilizável (GPT Image 2 + Topaz + ElevenLabs) ·
4. Talking-head (LTX/Kling/Seedance) · 5. Product demo · 6. Execução paralela ·
7. QC · 8. Montagem · 9. Distribuição · 10. Loop de feedback.

Cada passo é um node num `StateGraph` do LangGraph. Os adapters de provedores são
abstraídos por protocols e ligados por `config/providers.yaml`, sem mexer no grafo.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Uso

```bash
orchestrator run --batch 12 --offer "serum X" --config-dir config-mock  # dry-run sem rede
orchestrator status <run_id> --config-dir config-mock                   # relatório do run
orchestrator resume <run_id> --config-dir config-mock                   # retoma no mesmo thread_id
orchestrator list                                                   # lista runs
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config-mock  # loop de feedback mock
```

## Rodar o perfil live

```bash
cp .env.example .env
# preencha as chaves reais no .env:
# AI_GATEWAY_API_KEY, REPLICATE_API_TOKEN, REPLICATE_ELEVENLABS_MODEL
# e os campos/voice ids do modelo ElevenLabs hospedado no Replicate
```

O perfil `config/` já ativa `llm: vercel_gateway_llm`, `creator:
creator_real_replicate` e `video: replicate`. O vídeo usa LTX 2.3 Fast sem áudio
(`generate_audio: false`); a voz do creator vem de ElevenLabs via Replicate, e o
voiceover final entra em etapa posterior de montagem.

```bash
orchestrator run --batch 3 --offer "serum X" --config-dir config
```

Passo a passo completo, com a saída esperada de cada comando e como lê-la:
**[`docs/DEMO.md`](docs/DEMO.md)**.

## Testes (TDD)

```bash
pytest                                    # toda a suíte (determinística, sem rede)
pytest tests/test_judge_eval.py           # LLM Judge via cassette (CI)
pytest tests/test_judge_eval.py --live    # LLM Judge contra o gateway real (regrava cassette)
```

**Regra de integridade dos testes:** se um teste falha, investiga-se a causa raiz e corrige-se o
código — nunca se afrouxa a asserção só para passar. Ver `CLAUDE.md`.

## Documentação

- `CLAUDE.md` — guia para sessões do Claude Code (stack, convenções, regra dos testes).
- `docs/DECISIONS.md` — log de todas as decisões + rationale.
- `docs/PROGRESS.md` — handoff: o que está feito, o que falta, próximo passo.
# video-orchestration-ai
