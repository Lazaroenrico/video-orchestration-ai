# DEMO — testar o motor mock ponta a ponta

Guia para rodar e **ver** a pipeline de AI UGC em modo **mock/dry-run** (sem rede, custo
zero, determinístico). É a prova de conceito do motor: os 10 passos do `Context.md` rodam
como nodes do LangGraph e produzem um batch de "vídeos" fictícios com custo por tier, QC,
montagem, distribuição e o loop de feedback.

> Nada aqui chama API externa. Todas as URIs são `mock://...` e os custos são calculados
> a partir das tabelas de tier do `config/pipeline.yaml`.

## 1. Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Confirme que a suíte está verde (especificação executável do motor):

```bash
pytest
# 87 passed, 1 skipped, 1 warning
```

O único skip é o teste `--live` do LLM Judge (opt-in, exige um gateway externo). O warning
é benigno (comportamento interno do LangGraph ao cancelar tasks no resume parcial — ver
`docs/PROGRESS.md`, falha #5).

> Nota: o hook do `rtk` colapsa a saída do pytest. Para ver o resultado real, rode
> `rtk proxy python -m pytest`.

## 2. Rodar um batch

```bash
orchestrator run --batch 12 --offer "serum X" --run-id demo-run --config-dir config
```

```
run demo-run
  produzidos : 12
  aprovados  : 12
  descartados: 0
  em andamento: 0
  tentativas : 2
  custo mock : $3.68  {'ltx': 2.08, 'kling': 1.6}
  hooks top  : ['bold_claim', 'problem', 'emotional', 'social_proof']
```

### Como ler o relatório

| Campo          | Significado |
|----------------|-------------|
| `produzidos`   | itens que entraram no batch (= `--batch`) |
| `aprovados`    | passaram no QC e foram **distribuídos** (Step 9) |
| `descartados`  | esgotaram as tentativas de QC e nunca foram publicados (Step 7) |
| `em andamento` | não terminados (>0 só num run interrompido, antes de `resume`) |
| `tentativas`   | total de regenerações de QC somadas no batch (Step 7 loop) |
| `custo mock`   | custo total + quebra por tier (`cost_by_tier`) — Step 4 / "The Cost at Scale" |
| `hooks top`    | estilos de hook dos aprovados, ordenados por frequência — é isto que realimenta o Step 1 |

O custo por tier reflete o **roteamento escalonado**: a 1ª tentativa roda no LTX barato
($0.01/s), reprovas escalam para Kling ($0.10/s) e Seedance ($0.168/s). A maior parte do
volume fica no tier barato; só o que precisa de retry sobe — exatamente o "blended rate"
do `Context.md`.

### Mapa dos 10 passos → nodes

`concepts` (1) → `script` (2) → `roster`/creator (3) → talking-head `gen_<tier>` (4) →
`product_demo` (5) → fan-out paralelo via `Send` (6) → `qc` (7) → `assembly` (8) →
`distribution` (9) → `feedback` (10). Tudo em `src/orchestrator/nodes/stages.py`.

## 3. Inspecionar, listar e retomar

O estado de cada run fica checkpointado (sqlite); `thread_id = run_id`.

```bash
orchestrator status demo-run --config-dir config   # relê o relatório do checkpoint
orchestrator list                                   # lista os run_ids conhecidos
orchestrator resume demo-run --config-dir config    # retoma no mesmo thread_id
```

`status` e `resume` de um run já completo reproduzem o mesmo relatório do passo 2 (o run
terminou; não há nada pendente). O valor do `resume` aparece quando um batch é
interrompido no meio: os itens concluídos **não** re-executam, só os pendentes — o
checkpoint é granular por item.

## 4. O loop de feedback (Step 10 → Step 1)

É a parte que faz o sistema "se afiar" a cada ciclo. Rode N ciclos encadeados
compartilhando um `--feedback-store`: cada ciclo lê os hooks vencedores do anterior e os
usa como **viés** na geração de conceitos do próximo.

```bash
orchestrator loop --cycles 3 --batch 8 --offer "serum X" \
  --run-id-prefix demo --feedback-store fb.json --config-dir config
```

```
=== ciclo 1/3 ===
run demo-c1
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 6
  custo mock : $7.65  {'ltx': 1.76, 'kling': 3.2, 'seedance': 2.688}
  hooks top  : ['emotional', 'bold_claim', 'curiosity', 'problem']
=== ciclo 2/3 ===
run demo-c2
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 1
  custo mock : $2.16  {'ltx': 1.36, 'kling': 0.8}
  hooks top  : ['bold_claim', 'emotional', 'curiosity', 'social_proof']
=== ciclo 3/3 ===
run demo-c3
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 5
  custo mock : $6.22  {'ltx': 1.68, 'kling': 3.2, 'seedance': 1.344}
  hooks top  : ['emotional', 'curiosity', 'bold_claim', 'problem']
```

Repare nos `hooks top`: os vencedores do ciclo 1 (`emotional`, `bold_claim`, ...) puxam a
geração de conceitos do ciclo 2, que volta a alimentar o ciclo 3. O viés é uma fração
(~60%) dos conceitos — o resto mantém o spread, para o batch nunca virar "50 versões da
mesma ideia".

O store é um JSON acumulado por `run_id`, com um índice incremental `_idx` que define
"o mais recente" de forma determinística (não depende de timestamp de FS):

```json
{
  "demo-c1": { "_idx": 0, "produced": 8, "winning_styles": ["emotional", "bold_claim", "curiosity", "problem"], ... },
  "demo-c2": { "_idx": 1, "produced": 8, "winning_styles": ["bold_claim", "emotional", "curiosity", "social_proof"], ... },
  "demo-c3": { "_idx": 2, "produced": 8, "winning_styles": ["emotional", "curiosity", "bold_claim", "problem"], ... }
}
```

## 5. Determinismo

Mesmos inputs → **mesma saída**, sempre. Os mocks derivam tudo de hash dos inputs (sem
`random`); o id do item vem do id do conceito. Por isso os números acima são reproduzíveis
e os testes são estáveis. Mudar `--offer`, `--batch` ou o `--run-id` muda o resultado de
forma determinística.

## Limitações conhecidas (observabilidade)

O foco do v1 é o **motor**; a saída hoje é **agregada**. Ao testar, tenha em mente:

- O relatório **não lista o conteúdo por item** — os conceitos, scripts e URIs de clip
  por tier existem no estado (`Item.concept/script/clips/assembled`), mas não são
  expostos na CLI. Um relatório detalhado/export JSON seria o próximo incremento natural.
- `node_distribution` (Step 9) calcula um agendamento (conta + horário) no mock, mas só
  guarda `distributed: True` no estado — a agenda em si não é exibida.

Esses pontos são candidatos a um próximo passo, fora do escopo desta prova de conceito.
Ver `docs/PROGRESS.md` (próximos passos do v2) e `docs/DECISIONS.md`.
