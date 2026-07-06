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
      gen tier Step4
        ltx kling seedance
        generate_clip
        persist_item_media
      product_demo Step5
        generate_clip tier ltx fixo
      qc Step7
        qc_check
        route_after_qc
          pass to assembly
          fail e attempts menor max regen no tier seguinte
          fail e attempts esgotado drop
      assembly Step8
        assemble
        persist_item_media
      drop
        marca dropped true
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
      creator creator_real_replicate
        OpenAIImageAdapter via Vercel Gateway GPT Image 2
        ReplicateUpscaleAdapter real-esrgan
        ReplicateVoiceAdapter ElevenLabs TTS via Replicate
      video replicate
        LTX 2.3 Fast sem audio
        Kling e Seedance fallback mock
      qc integrity_qc
        bloqueia midia mock ou fallback antes da montagem
      assembly vercel_seedance_assembly
        video final Seedance 2.0 via Vercel AI Gateway
      judge gateway
        JudgePort via HTTP configurável judge.yaml
```

## 2. Diagrama de sequência das requisições externas

```mermaid
sequenceDiagram
    participant U as Usuário/CLI/Web
    participant G as LangGraph runner
    participant LLM as Vercel Gateway (Claude Opus 4.8)
    participant IMG as Vercel Gateway (GPT Image 2)
    participant REP as Replicate (upscale + ElevenLabs TTS + video)
    participant MEDIA as media_store (disco local)

    U->>G: run(offer, batch, platform, creator_prompt, video_prompt)
    G->>LLM: generate_concepts(offer, n, seed, bias)
    LLM-->>G: concepts[]
    par scripts por conceito
        G->>LLM: write_script(concept, creator_ref="creator", platform)
        LLM-->>G: script
    end
    opt run.edit_concepts
        G-->>U: interrupt edit_concepts (concept + script)
        U-->>G: conceitos editados/incluidos
    end
    G->>IMG: generate_face(index, system_prompt) [roster, N vezes em paralelo]
    IMG-->>G: primary (data URI) + angles
    G->>REP: upscale(primary) [real-esrgan]
    REP-->>G: upscaled_base URL
    G->>REP: create_voice(index) [ElevenLabs TTS]
    REP-->>G: voice_id
    G->>MEDIA: persist_creator_media (baixa bytes, reescreve URIs locais)
    par fan-out por item (max_concurrency)
        G->>REP: generate_clip LTX 2.3 Fast (image-to-video, sem audio)
        REP-->>G: clip mp4
        G->>G: qc_check (integrity_qc: bloqueia mídia mock/fallback)
        G->>MEDIA: persist_item_media (clips, assembled)
        G->>LLM: assemble → Seedance 2.0 (vercel_seedance_assembly, vídeo final)
    end
    G-->>U: feedback (summary agregando resultados do batch)
```

## 3. Tabela: stage → provider real hoje

| Step | Node | Provider configurado | Requisição externa? |
|------|------|----------------------|----------------------|
| 1 | `node_concepts` | `vercel_gateway_llm` | Sim — Claude Opus 4.8 via Vercel AI Gateway |
| 2 | `node_scripts` | `vercel_gateway_llm` | Sim — Claude Opus 4.8 via Vercel AI Gateway |
| 2.5 | `node_concept_review` | — | `interrupt()` humano (opcional, `run.edit_concepts`) |
| 3 | `node_roster` → `build_creator` | `creator_real_replicate` | Sim — Vercel Gateway (GPT Image 2), Replicate (upscale + ElevenLabs TTS) |
| 3.5 | `node_approval` | — | `interrupt()` humano (opcional, `run.approve_creators`) |
| 4 | `make_gen_node(tier)` | `replicate` | Sim para `ltx` — LTX 2.3 Fast image-to-video sem áudio; `kling`/`seedance` fallback mock |
| 5 | `node_product_demo` | `replicate` | Sim — LTX 2.3 Fast image-to-video sem áudio |
| 7 | `node_qc` | `integrity_qc` | Não — valida mídia real e bloqueia URIs mock/fallback antes da montagem |
| 8 | `node_assembly` | `vercel_seedance_assembly` | Sim — vídeo final Seedance 2.0 (`bytedance/seedance-2.0`) via Vercel AI Gateway |
| — | `JudgePort` (gateway) | `gateway` | Sim, quando usado — HTTP configurável (`config/judge.yaml`) |

## 4. Notas de arquitetura

- **Topologia fixa, comportamento por config**: o grafo (`graph/builder.py`) não
  muda entre mock e real — só `config/providers.yaml` troca o adapter por role
  (`registry.py` resolve provider → implementação).
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
