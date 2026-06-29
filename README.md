# UGC Orchestrator

Motor de orquestração para a pipeline de **AI UGC em escala** (500+ vídeos/semana) descrita em
`Context.md`. **v1 = só o motor**, em modo **mock/dry-run** (nenhuma chamada externa real, custo zero),
construído via **TDD** sobre **LangGraph / LangChain / LangSmith**.

## Pipeline (10 passos)

1. Conceitos (Claude) · 2. Scripts (Claude) · 3. Creator reutilizável (GPT Image 2 + Topaz + ElevenLabs) ·
4. Talking-head (LTX/Kling/Seedance) · 5. Product demo · 6. Execução paralela ·
7. QC · 8. Montagem · 9. Distribuição · 10. Loop de feedback.

No v1 cada passo é um **node mock** num `StateGraph` do LangGraph. Os adapters de provedores são
abstraídos (LangChain `Runnable`s) e plugados de verdade depois, node a node.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Uso

```bash
orchestrator run --batch 12 --offer "serum X" --config-dir config   # pipeline mock ponta a ponta
orchestrator status <run_id> --config-dir config                    # relatório do run (lê o checkpoint)
orchestrator resume <run_id> --config-dir config                    # retoma no mesmo thread_id
orchestrator list                                                   # lista runs
orchestrator loop --cycles 3 --feedback-store fb.json --config-dir config  # N ciclos encadeados (loop de feedback)
```

## Ativar LLM via Vercel AI Gateway

```bash
cp .env.example .env
# preencha AI_GATEWAY_API_KEY no .env
```

Em `config/providers.yaml`, troque `llm: mock` por `llm: vercel_gateway_llm`, depois rode:

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
