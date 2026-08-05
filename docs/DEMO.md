# DEMO — testar o motor mock ponta a ponta

Guia para rodar e **ver** a pipeline de AI UGC em modo **mock/dry-run** usando
`--config-dir config-mock` (sem rede, custo zero, determinístico). É a prova de
conceito do motor: a pipeline atual roda como nodes do LangGraph e
produzem um batch de vídeos fictícios/renderizáveis com custo por tier, QC,
montagem e loop de feedback.

> Nada aqui chama API externa. As mídias mock usam `data:` renderizável quando possível,
> e os custos são calculados a partir das tabelas de tier do `config-mock/pipeline.yaml`.
> O diretório `config/` é live/híbrido e pode chamar APIs reais.

## Ambiente dev durável pela UI

O fluxo recomendado para testar API, runner, PostgreSQL e frontend juntos é:

```bash
cp .env.example .env
# preencha as credenciais de R2 no .env
ORCH_DEV_CONFIG_DIR=config-staging ./scripts/dev-local up
```

Isso sobe a aplicação em `http://localhost:5173`, a API em
`http://localhost:8000` e o PostgreSQL local em `127.0.0.1:55432`. O staging usa
adapters de geração mock, mas grava runs, jobs, o gate `review_creative_plan`,
eventos SSE e checkpoints na mesma infraestrutura durável do perfil live.

Para o smoke de retomada:

1. Confirme `GET http://localhost:8000/readyz`.
2. Crie uma campanha pela UI e aguarde a fase **Revisão**.
3. Abra o detalhe da campanha em outra aba para confirmar o replay SSE.
4. Reinicie somente o serviço `api` com Docker Compose e recarregue a página.
5. Aprove o plano criativo; o runner embutido retoma o job a partir do PostgreSQL.

`./scripts/dev-local down` preserva o banco. `./scripts/dev-local reset --yes`
remove banco e volumes locais, mas nunca toca no bucket R2. Para testar sem R2:

```bash
ORCH_DEV_CONFIG_DIR=config-staging ORCH_DEV_STORAGE_BACKEND=local \
  ./scripts/dev-local up
```

O teste live usa o mesmo comando sem `ORCH_DEV_CONFIG_DIR`, cria uma campanha com
batch 1 e chama somente Vercel AI Gateway, Replicate (clips + voz) e Cloudflare
R2. Antes da campanha, configure as quotas locais em outro terminal com
`./scripts/dev-local quotas --design-chars 500 --voice-slots 2 --tts-chars 1000`.
A montagem roda localmente com FFmpeg.

## 1. Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

Confirme que a suíte está verde (especificação executável do motor):

```bash
pytest
# 537 passed, 2 skipped, 2 warnings (cobertura 100%, gate fail_under=100)
```

Os 2 skips são testes `--live` (opt-in, exigem gateway externo). Os warnings são benignos
(deprecation de import do LangSmith; comportamento interno do LangGraph ao cancelar tasks no
resume parcial — ver a [falha #5 no histórico de junho](progress/archive/2026-06.md#falhas-de-teste-investigadas-sintoma--causa-raiz--correção)).

> Nota: o hook do `rtk` colapsa a saída do pytest. Para ver o resultado real, rode
> `rtk proxy python -m pytest`.

## 2. Rodar um batch

```bash
orchestrator run --batch 12 --offer "serum X" --run-id demo-run --config-dir config-mock
```

```
run demo-run
  produzidos : 12
  aprovados  : 12
  descartados: 0
  em andamento: 0
  tentativas : 2
  custo total: $3.6800  {'video': 3.68, 'voiceover': 0.0, 'assembly': 0.0}
  hooks top  : ['bold_claim', 'problem', 'emotional', 'social_proof']
```

### Como ler o relatório

| Campo          | Significado |
|----------------|-------------|
| `produzidos`   | itens que entraram no batch (= `--batch`) |
| `aprovados`    | passaram no QC e geraram vídeo final em `assembly` |
| `descartados`  | esgotaram as tentativas de QC e nunca chegaram ao vídeo final |
| `em andamento` | não terminados (>0 só num run interrompido, antes de `resume`) |
| `tentativas`   | total de regenerações de QC somadas no batch (Step 7 loop) |
| `custo total`  | custo total + quebra por etapa (`cost_by_stage`), incluindo locução |
| `hooks top`    | estilos de hook dos aprovados, ordenados por frequência — é isto que realimenta o Step 1 |

No `config-mock`, o custo por tier reflete o roteamento LTX-only do dry-run: a primeira
tentativa e as reprovas de QC permanecem no primeiro tier. No perfil live `config/`,
talking-head, regenerações de QC e product demo usam `prunaai/p-video` via
Replicate em draft 1080p. Depois do QC, ElevenLabs gera a locução aprovada e o
FFmpeg concatena os dois clips, descarta o áudio de origem e entrega H.264/AAC.

### Mapa dos 9 passos → nodes

`concepts` (1) → `script` (2) → `roster`/creator (3) → talking-head `gen_<tier>` (4) →
`product_demo` (5) → fan-out paralelo via `Send` (6) → `qc` (7) →
`voiceover` (8) → `assembly` (9) → `feedback` (10). Tudo em
`src/orchestrator/nodes/stages.py`.

## 3. Inspecionar, listar e retomar

O estado de cada run fica checkpointado (sqlite); `thread_id = run_id`.

```bash
orchestrator status demo-run --config-dir config-mock   # relê o relatório do checkpoint
orchestrator list                                   # lista os run_ids conhecidos
orchestrator resume demo-run --config-dir config-mock    # retoma no mesmo thread_id
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
  --run-id-prefix demo --feedback-store fb.json --config-dir config-mock
```

```
=== ciclo 1/3 ===
run demo-c1
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 6
  custo total: $7.6480  {'video': 7.648, 'voiceover': 0.0, 'assembly': 0.0}
  hooks top  : ['emotional', 'bold_claim', 'curiosity', 'problem']
=== ciclo 2/3 ===
run demo-c2
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 1
  custo total: $2.1600  {'video': 2.16, 'voiceover': 0.0, 'assembly': 0.0}
  hooks top  : ['bold_claim', 'emotional', 'curiosity', 'social_proof']
=== ciclo 3/3 ===
run demo-c3
  produzidos : 8
  aprovados  : 8
  descartados: 0
  em andamento: 0
  tentativas : 5
  custo total: $6.2240  {'video': 6.224, 'voiceover': 0.0, 'assembly': 0.0}
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

## 6. Dashboard: scripts e Draft Video

O dashboard React consegue retomar a visualização de scripts a partir do checkpoint, não
apenas do SSE ao vivo. A tela `/scripts` também aceita um run explícito:

```text
http://localhost:8000/scripts?run=<run_id>
```

Fluxo para criar um rascunho com um creator já aprovado/recuperado:

1. Abra `/creators`.
2. Selecione um creator com imagem e voz completas.
3. Preencha `Product / Offer` no drawer, se quiser sobrescrever a oferta original.
4. Clique `Draft Video with creator-*`.
5. A UI cria um novo run usando esse creator como roster fixo e navega para
   `/scripts?run=<novo_run_id>`.
6. Edite ou exclua conceitos/scripts e clique `Save & Continue`; o run segue para vídeo,
   QC e montagem com `creator_ref` apontando para o creator escolhido.

## Limitações conhecidas (observabilidade)

O foco do v1 é o **motor**; a saída hoje é **agregada**. Ao testar, tenha em mente:

- O relatório **não lista o conteúdo por item** — os conceitos, scripts e URIs de clip
  por tier existem no estado (`Item.concept/script/clips/assembled`) e agora são expostos
  ao dashboard via `/api/state/{run_id}`, mas a CLI ainda mostra apenas o resumo agregado.
  Um relatório detalhado/export JSON seria o próximo incremento natural para terminal.
- Não há etapa de distribuição/postagem no motor atual; o item aprovado termina em
  `node_assembly`, com `assembled` preenchido.

Esses pontos são candidatos a um próximo passo, fora do escopo desta prova de conceito.
Veja o registro legado de [próximos passos do v2](progress/archive/2026-06.md#próximos-passos-v2-pós-mvp),
o [painel atual](PROGRESS.md) e as [decisões](DECISIONS.md).
