# Fluxo atual da pipeline — mindmap visual

Gerado a partir do estado atual do código (`src/orchestrator/`). Cobre: grafo
LangGraph (topo + subgrafo por item), quem chama quem, e quais requisições
externas cada stage dispara hoje segundo `config/providers.yaml`.

## 1. Visão geral (mindmap)

```mermaid
mindmap
  root((Pipeline AI UGC))
    Grafo de topo BatchState
      concepts Step1
        generate_concepts LLM
      scripts Step2
        write_script por conceito
      concept_review Step2.5
        interrupt humano opcional
      roster Step3
        build_creator x N paralelo
        persist_creator_media
        voice_preview
      approval Step3.5
        interrupt humano opcional
      fan-out Send
        1 Item por concepto
        move concept script para Item.script
        creator_ref round-robin do roster
        creator_image_uri para video image-to-video
      process_item
        invoca subgrafo Item
      feedback Step10
        agrega resultados
        salva feedback_store
        alimenta bias do proximo ciclo
    Subgrafo per-item Item
      route_after_script
        escolhe tier conforme attempts
      gen tier configurado Step4
        pruna no perfil live
        generate_clip
        persist_item_media
      product_demo Step5
        generate_clip tier video.product_demo_tier
      qc Step7
        qc_check
        route_after_qc
          pass to assembly
          fail e attempts menor max regen no tier configurado
          fail e attempts esgotado drop
      assembly Step8
        assemble
        persist_item_media
      drop
        marca dropped true
    Camada de tools
      nodes chamam tools tipadas
      tools validam shape de adapter output
      tools delegam ao CompositeAdapter ja resolvido
      registry estatico prepara roteamento futuro de agents
    Camada web FastAPI
      SPA React front dist
        GET serve index Kinetic Command 12 telas
        catch-all rotas client-side sem sombrear api media videos assets
      POST /api/run
        dispara _execute_run em background
      GET /api/stream/run_id
        SSE token_cb via stream_bus
      POST /api/approve/run_id
        resolve o interrupt de approval
      POST /api/approve/run_id/creators/creator_id/reroll-voice
      GET /api/creators
      GET /api/prompts POST DELETE
      GET /api/integrations
        mapa stage adapter de providers.yaml
      GET /api/runs
      GET /api/status/run_id
    Adapters e requisicoes externas hoje
      llm vercel_gateway_llm
        AnthropicLLMAdapter
        Claude Opus 4.8 via Vercel AI Gateway
        generate_concepts e write_script
      creator creator_vercel_replicate_voice
        OpenAIImageAdapter via Vercel Gateway GPT Image 2
        ReplicateVoiceAdapter ElevenLabs TTS via Replicate
      video replicate
        PrunaAI P-Video para talking-head
        PrunaAI P-Video para product demo
      qc integrity_qc
        bloqueia midia mock ou fallback antes da montagem
      assembly replicate
        video final PrunaAI P-Video em draft 1080p
      judge gateway
        JudgePort via HTTP configurável judge.yaml
```

### Camada `nodes -> tools -> adapters`

Os nodes de `src/orchestrator/nodes/stages.py` não chamam mais métodos do adapter
diretamente. Cada stage monta um `ToolContext` a partir do `RunnableConfig` e chama
uma tool fina em `src/orchestrator/tools/`; a tool adiciona metadata de tracing,
valida o shape retornado e só então devolve o tipo que o node já esperava. O
`CompositeAdapter` continua sendo a fonte de roteamento por papel (`llm`,
`creator`, `video`, `qc`, `assembly`, `upscale`), sem mudar a topologia LangGraph.

## 2. Diagrama de sequência das requisições externas

```mermaid
sequenceDiagram
    participant U as Usuário/CLI/Web
    participant G as LangGraph runner
    participant T as Tools tipadas
    participant LLM as Vercel Gateway (Claude Opus 4.8)
    participant IMG as Vercel Gateway (GPT Image 2)
    participant REP as Replicate (ElevenLabs TTS)
    participant VID as Vercel Gateway (Seedance)
    participant MEDIA as media_store (disco local)

    U->>G: run(offer, batch, platform, creator_prompt, video_prompt)
    G->>T: generate_concepts_tool(offer, n, seed, bias)
    T->>LLM: generate_concepts(offer, n, seed, bias)
    LLM-->>G: concepts[]
    par scripts por conceito
        G->>T: write_script_tool(concept, creator_ref="creator", platform)
        T->>LLM: write_script(concept, creator_ref="creator", platform)
        LLM-->>G: script
    end
    opt run.edit_concepts
        G-->>U: interrupt edit_concepts (concept + script)
        U-->>G: conceitos editados/incluidos
    end
    G->>T: build_creator_tool(index, system_prompt) [roster, N vezes em paralelo]
    T->>IMG: generate_face(index, system_prompt)
    IMG-->>G: primary (data URI) + angles
    G->>REP: create_voice(index) [ElevenLabs TTS]
    REP-->>G: voice_id
    G->>MEDIA: persist_creator_media (baixa bytes, reescreve URIs locais)
    par fan-out por item (max_concurrency)
        G->>T: generate_clip_tool(tier, item, prompt, reference)
        T->>VID: Replicate PrunaAI P-Video (talking-head, sem audio)
        T->>VID: Replicate PrunaAI P-Video (product demo, sem audio)
        VID-->>G: clips mp4
        G->>T: qc_check_tool(item)
        T->>G: qc_check (integrity_qc: bloqueia mídia mock/fallback)
        G->>MEDIA: persist_item_media (clips, assembled)
        G->>T: assemble_video_tool(item, platform, prompt)
        T->>VID: assemble → Replicate PrunaAI P-Video (draft, vídeo final)
    end
    G-->>U: feedback (summary agregando resultados do batch)
```

## 3. Tabela: stage → provider real hoje

| Step | Node | Tool | Provider configurado | Requisição externa? |
|------|------|------|----------------------|----------------------|
| 1 | `node_concepts` | `generate_concepts_tool` | `vercel_gateway_llm` | Sim — Claude Opus 4.8 via Vercel AI Gateway |
| 2 | `node_scripts` | `write_script_tool` | `vercel_gateway_llm` | Sim — Claude Opus 4.8 via Vercel AI Gateway |
| 2.5 | `node_concept_review` | — | — | `interrupt()` humano (opcional, `run.edit_concepts`) |
| 3 | `node_roster` | `build_creator_tool` | `creator_vercel_replicate_voice` | Sim — Vercel Gateway (GPT Image 2), Replicate (somente ElevenLabs TTS) |
| 3.5 | `node_approval` | — | — | `interrupt()` humano (opcional, `run.approve_creators`) |
| 4 | `make_gen_node(tier)` | `generate_clip_tool` | `replicate` | Sim — PrunaAI P-Video sem áudio via Replicate |
| 5 | `node_product_demo` | `generate_clip_tool` | `replicate` | Sim — PrunaAI P-Video sem áudio via Replicate |
| 7 | `node_qc` | `qc_check_tool` | `integrity_qc` | Não — valida mídia real e bloqueia URIs mock/fallback antes da montagem |
| 8 | `node_assembly` | `assemble_video_tool` | `replicate` | Sim — vídeo final PrunaAI P-Video em draft 1080p via Replicate |
| 8 | `node_upscale` | `upscale_video_tool` | `passthrough_upscale` | Não no perfil atual — role existe para plugar upscale real depois |
| — | `JudgePort` (gateway) | — | `gateway` | Sim, quando usado — HTTP configurável (`config/judge.yaml`) |

## 4. Notas de arquitetura

- **Topologia fixa, comportamento por config**: o grafo (`graph/builder.py`) não
  muda entre mock e real — só `config/providers.yaml` troca o adapter por role
  (`registry.py` resolve provider → implementação).
- **Tools finas antes dos adapters**: `tools/` é uma camada de contrato e tracing,
  não um runtime de agents. Ela recebe o adapter já resolvido pelo grafo e valida
  outputs (`Artifact`, `QCResult`, `dict`, `str`) antes de o node persistir mídia ou
  decidir rotas.
- **Retry**: chamadas HTTP passam por `adapters/_retry.py`
  (`with_transport_retry`), que retenta `httpx.TransportError`, `ReplicateError`
  429 e `httpx.HTTPStatusError` 429; outros status (401/422/500) propagam na 1ª
  tentativa.
- **Streaming para UI**: `stream_bus.emit_token` empurra eventos
  (`creator_start`, `creator_ready`, etc.) consumidos via SSE em
  `GET /api/stream/{run_id}` no `web/server.py`.
- **Persistência de mídia**: `media_store.py` baixa bytes remotos (imagem,
  voz, clipes) e reescreve URIs para caminhos locais servíveis sob
  `/media/{run_id}/...`, tornando o dashboard independente das URLs
  originais dos providers.
- **QC loop**: `route_after_qc` decide entre reprocessar no tier configurado,
  ir para `assembly`, ou `drop` após `qc.max_attempts` (default 3).
- **Feedback loop (Step 10 → 1)**: `node_feedback` grava um resumo em
  `feedback_store`; o próximo ciclo (`orchestrator loop`) usa
  `prior_winning_styles` como `bias` em `generate_concepts`.
