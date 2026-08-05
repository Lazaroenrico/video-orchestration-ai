# Plano — voz do creator derivada da identidade visual

Status: proposto, ainda não implementado  
Data: 2026-07-29  
Escopo: criação da voz, revisão humana, persistência, TTS e custo. A geração de
clips Replicate/PrunaAI e a montagem FFmpeg permanecem inalteradas.

## 1. Objetivo

Eliminar a configuração manual de nomes/IDs de vozes no `.env`:

```dotenv
REPLICATE_ELEVENLABS_VOICE_ID=
REPLICATE_ELEVENLABS_VOICE_ID_FEMALE=
REPLICATE_ELEVENLABS_VOICE_ID_MALE=
REPLICATE_ELEVENLABS_VOICE_ID_NEUTRAL=
```

Cada creator deve ganhar uma voz própria, coerente com sua identidade visual e
criativa. Essa voz será criada uma vez, aprovada dentro do gate humano V2 e
reutilizada em todas as locuções futuras daquele creator.

O `.env` continuará contendo somente credenciais:

```dotenv
ELEVENLABS_API_KEY=
REPLICATE_API_TOKEN=
```

`REPLICATE_API_TOKEN` continua necessário para os clips PrunaAI. A criação e a
síntese da voz passam a usar a API direta do ElevenLabs.

## 2. Estado atual e motivo da mudança

Hoje, `CreatorProfileSubmission` já possui:

- `visual_brief`;
- `voice_brief`;
- `performance_style`;
- `exclusions`.

`node_roster` combina esses textos em um `VoiceProfile`, usa o mesmo preset para
o prompt de imagem e para a voz e, finalmente, `ReplicateVoiceAdapter` escolhe
uma entrada dos pools `female`, `male`, `neutral` ou `default` do `.env`.

Esse desenho mantém paridade básica entre imagem e voz, mas ainda tem três
limitações:

1. a voz exata é cadastrada manualmente no ambiente;
2. creators diferentes podem reutilizar o mesmo pequeno conjunto de vozes;
3. a imagem renderizada não participa da decisão, apenas o briefing anterior a
   ela.

O modelo `elevenlabs/turbo-v2.5` no Replicate é TTS: recebe texto e uma voz já
existente. Ele não oferece o fluxo de Voice Design. Portanto, remover os pools
sem trocar o caminho de criação deixaria o adapter sem uma referência de voz.

## 3. Decisão arquitetural

A fonte canônica será uma identidade estruturada do creator. A imagem influencia
a voz, mas não será usada isoladamente para adivinhar características pessoais.

```text
CreatorProfile + imagem renderizada + configuração da campanha
                           │
                           ▼
                    VoiceMatchSpec
                           │
                           ▼
                 ElevenLabs Voice Design
                           │
                  3 previews no R2
                           │
                           ▼
              gate review_creative_plan
                           │
               voz escolhida pelo usuário
                           │
                           ▼
              voice_id permanente do creator
                           │
                           ▼
               TTS ElevenLabs → FFmpeg
```

Para creators gerados pela aplicação, o perfil criativo continua sendo a fonte
principal e a imagem funciona como sinal de coerência visual. Para um
`seed_creator` enviado pelo usuário, uma análise visual limitada produz somente
atributos estéticos permitidos; idioma, sotaque e outras escolhas que não podem
ser deduzidas de uma foto vêm da campanha ou de um override explícito.

Essa mudança preserva:

- LangGraph como orquestrador canônico;
- chamadas por typed tools;
- `CompositeAdapter` como roteador por papel;
- exatamente um interrupt humano, `review_creative_plan`;
- ponteiros R2 canônicos no estado/banco;
- perfis mock e staging sem custo externo;
- o contrato de voz estável separado da URL temporária de preview.

## 4. Propriedade e precedência dos atributos

| Atributo | Fonte canônica | Imagem pode sugerir? | Observação |
|---|---|---:|---|
| idioma | campanha, server-owned | não | Ex.: `pt-BR`; nunca inferir por aparência |
| sotaque | campanha ou usuário | não | Default neutro do locale |
| apresentação vocal | perfil criativo ou usuário | parcialmente | Descrição de performance, não identidade de gênero |
| faixa vocal | perfil criativo | não para seed image | Ex.: jovem adulta, adulta, madura como direção de atuação |
| timbre | perfil + sinais estéticos | sim | Ex.: quente, claro, encorpado |
| ritmo | performance da campanha | sim | Ex.: conversacional, rápido, deliberado |
| energia | performance + contexto visual | sim | Ex.: calma, confiante, entusiasmada |
| emoção | roteiro/performance | sim | Não deve trocar a identidade base da voz |
| etnia, saúde, deficiência ou orientação | nenhuma | nunca | Fora do contrato |

Precedência:

1. override explícito do usuário;
2. valores server-owned da campanha;
3. `voice_brief` e `performance_style`;
4. sugestões visuais permitidas;
5. defaults seguros e determinísticos.

## 5. Novos contratos

Adicionar contratos estritos em `src/orchestrator/creative_contracts.py`.

### 5.1 `CreatorVoiceSpec`

Shape proposto:

```python
class CreatorVoiceSpec(StrictModel):
    language_code: str
    accent: str
    vocal_presentation: Literal[
        "feminine", "masculine", "androgynous", "neutral"
    ]
    vocal_age: Literal["young_adult", "adult", "mature"]
    timbre: Literal["light", "clear", "warm", "full", "deep"]
    pace: Literal["calm", "conversational", "energetic"]
    energy: Literal["low", "balanced", "high"]
    warmth: float = Field(ge=0, le=1)
    expressiveness: float = Field(ge=0, le=1)
    use_case: Literal["ugc_social"]
    rationale: str = Field(max_length=500)
```

Regras:

- `language_code`, `use_case` e limites são server-owned;
- valores vindos do modelo são validados por enum/range;
- `rationale` pode aparecer no gate, mas não entra em tracing;
- o prompt final do provider é montado pelo servidor;
- `voice_brief` continua existindo para compatibilidade e edição humana;
- `VoiceProfile` atual permanece durante a migração e vira um mapper legado para
  `CreatorVoiceSpec`.

### 5.2 Candidatos

```python
class VoiceCandidate(StrictModel):
    candidate_id: str
    preview: Artifact
    duration_seconds: float
    media_type: str


class VoiceDesignBatch(StrictModel):
    provider: Literal["elevenlabs"]
    design_model: str
    description_hash: str
    prompt_version: str
    candidates: list[VoiceCandidate] = Field(min_length=1, max_length=3)
    cost_usd: float = Field(ge=0)
```

O `candidate_id` é opaco. O frontend nunca envia um `voice_id` arbitrário:
apenas seleciona um candidato presente no payload canônico do gate.

### 5.3 Voz final

```python
class FinalizedVoice(StrictModel):
    provider: Literal["elevenlabs"]
    voice_ref: str
    selected_candidate_id: str
    preview_uri: str
    design_model: str
    tts_model: str
```

`voice_ref` continua sendo a referência estável usada pelo fan-out e por
`node_voiceover`. `preview_uri` continua sendo um artifact R2 reproduzível.

## 6. Ports, adapters e typed tools

### 6.1 `VoiceDesignPort`

Adicionar a `adapters/base.py`:

```python
class VoiceDesignPort(Protocol):
    async def design_voice_candidates(...) -> VoiceDesignBatch: ...
    async def finalize_voice(...) -> FinalizedVoice: ...
    async def synthesize_voiceover(...) -> Artifact: ...
```

`VoicePort` atual fica disponível no modo legado até o rollout terminar.

### 6.2 `ElevenLabsVoiceDesignAdapter`

Criar `src/orchestrator/adapters/elevenlabs_voice_design.py`, usando
`httpx.AsyncClient` injetável:

1. `POST /v1/text-to-voice/design`
   - modelo inicial: `eleven_ttv_v3`;
   - recebe descrição de 20–1000 caracteres;
   - usa texto de preview em português com 100–1000 caracteres;
   - valida todos os previews e `generated_voice_id`;
   - converte o áudio base64 em bytes transitórios;
   - nunca devolve base64 para o checkpoint.
2. `POST /v1/text-to-voice`
   - chamado somente depois da aprovação;
   - cria a voz permanente com nome determinístico;
   - valida `voice_id`, nome e resposta.
3. `POST /v1/text-to-speech/{voice_id}`
   - modelo inicial: `eleven_turbo_v2_5`;
   - sintetiza previews persistidos e a locução final;
   - captura `character-cost`, `request-id` e `x-trace-id` quando disponíveis.

O nome enviado ao provider deve permitir reconciliação:

```text
ugc-{organization_id}-{creator_id}-{description_hash[:10]}
```

Antes de salvar novamente uma voz depois de retry/resume, o adapter/repositório
consulta a voz já registrada por referência determinística. Isso evita consumir
dois voice slots para o mesmo creator.

### 6.3 Typed tools

Registrar:

- `derive_creator_voice_spec`
  - papel `llm`;
  - recebe imagem assinada de forma transitória, perfil e campos server-owned;
  - devolve somente `CreatorVoiceSpec`.
- `design_creator_voice`
  - papel `creator`;
  - recebe `CreatorVoiceSpec` validado;
  - devolve `VoiceDesignBatch`.
- `finalize_creator_voice`
  - papel `creator`;
  - recebe um candidato pertencente ao gate;
  - devolve `FinalizedVoice`.
- manter `synthesize_voiceover`
  - passa a delegar ao adapter ElevenLabs direto.

Nodes e agents não chamam adapters diretamente. Os schemas devem constar no
`TOOL_REGISTRY` com papel, stage e descrição.

## 7. Fluxo LangGraph

Fluxo interno proposto:

```text
concepts
  → scripts
  → creator_profiles
  → roster_images
  → voice_candidates
  → review
       ├─ approve → finalize_voices → fan-out de produção
       ├─ regenerate voices → voice_candidates → review
       ├─ regenerate creators → creator_profiles → ...
       ├─ regenerate scripts → scripts → ...
       └─ regenerate concepts → concepts → ...
```

As cinco fases públicas não mudam. `roster_images` e `voice_candidates` aparecem
como progresso interno de “Plano criativo/Revisão”.

### 7.1 `roster_images`

Refatorar o `node_roster` atual:

- gerar e persistir a imagem;
- não criar voz definitiva;
- preservar `voice_brief`, `performance_style` e overrides;
- para `seed_creator`, validar/persistir a imagem normalmente.

### 7.2 `voice_candidates`

Para cada creator:

1. derivar `CreatorVoiceSpec`;
2. gerar três candidatos;
3. persistir cada preview como `voice_candidate` no backend selecionado;
4. substituir bytes/URLs temporárias por `r2://`;
5. guardar `description_hash`, `prompt_version` e custo;
6. emitir SSE incremental sem corpo do prompt.

Regenerar `target=voices` preserva:

- imagem;
- IDs e assignments do creator;
- conceitos e scripts;
- candidatos anteriores como histórico de custo;
- `voice_reroll_count`.

### 7.3 `review`

Continua existindo exatamente um `interrupt`:

```text
review_creative_plan
```

O payload passa a incluir, por creator:

- imagem;
- `voice_brief`;
- resumo do `CreatorVoiceSpec`;
- três previews assinados somente na resposta;
- candidato recomendado;
- custo acumulado de Voice Design.

### 7.4 `finalize_voices`

Executa depois da aprovação e antes do fan-out:

- valida que cada candidato pertence à versão canônica do gate;
- cria somente a voz selecionada na biblioteca ElevenLabs;
- persiste `voice_ref`;
- preserva o preview escolhido;
- atualiza custo e metadata;
- impede produção se qualquer creator atribuído a um item ficar sem voz;
- usa o ledger de efeitos para sobreviver a retries/restart.

Quando `review_plan=false`, a política automática é selecionar deterministicamente
o primeiro candidato válido e seguir por `finalize_voices`. Em `config-mock` e
`config-staging`, candidatos e voz final são mocks determinísticos e custam zero.

## 8. Gate e contrato HTTP V2

### 8.1 Separar request de response

Hoje `ReviewCreatorPatch` aceita campos derivados como `voice_ref` e
`voice_preview_uri`. O plano deve separar:

- `ReviewCreatorResponse`: pode expor previews assinados e metadata;
- `ReviewCreatorPatch`: aceita apenas campos editáveis.

Campos novos de request:

```python
selected_voice_candidate_id: Optional[str]
voice_brief: Optional[str]
```

Campos server-owned e não editáveis:

- `voice_ref`;
- provider/model;
- custo;
- URI R2 canônica;
- hash/version do prompt;
- IDs de candidatos que não estejam no gate.

### 8.2 Validações

No `POST /api/v2/runs/{run_id}/review`:

- `approve` exige uma seleção válida por creator aprovado;
- o candidato precisa pertencer ao `gate_id` e `version`;
- candidato de outro creator retorna 422;
- mudança de `voice_brief` invalida candidatos antigos e exige regeneração;
- stale continua retornando 409;
- gate cancelado continua retornando 410;
- `regenerate` passa a aceitar `target="voices"` e `ids`;
- reroll de voz não regenera imagem.

Não será criado um segundo gate.

## 9. Frontend

Atualizar o card de cada creator na revisão:

- imagem à esquerda;
- `archetype`, `performance_style` e `voice_brief` editáveis;
- resumo da voz proposta;
- três players de áudio com seleção única;
- badge “recomendada” no candidato inicial;
- ação “Gerar outras vozes” por creator;
- contador de rerolls e custo estimado;
- estado de carregamento por creator;
- erro acionável sem esconder os previews válidos;
- botão “Aprovar plano” desabilitado enquanto faltar seleção.

Ao editar `voice_brief`, a UI marca os previews como desatualizados e exige
“Gerar outras vozes” antes da aprovação.

O frontend recebe apenas URLs assinadas derivadas pela API. Nunca persiste uma
signed URL nem envia `voice_ref` inventado.

## 10. Persistência e migração

Criar migração Alembic `20260729_0010_creator_voice_design.py`.

Campos propostos em `creators`:

```text
voice_spec                 JSONB NOT NULL DEFAULT '{}'
voice_provider             TEXT NULL
voice_design_model         TEXT NULL
voice_tts_model            TEXT NULL
voice_design_hash          TEXT NULL
voice_selected_candidate   TEXT NULL
voice_status               TEXT NOT NULL DEFAULT 'legacy'
voice_design_meta          JSONB NOT NULL DEFAULT '{}'
```

Constraint de `voice_status`:

```text
legacy | candidates_ready | selected | failed
```

Compatibilidade:

- não alterar `voice_ref` ou `voice_preview_uri` históricos;
- registros existentes recebem `voice_status=legacy`;
- não tentar inferir provider de referências antigas;
- loaders antigos continuam funcionando com defaults;
- downgrade remove somente as novas colunas;
- RLS e chave tenant-scoped permanecem iguais.

### 10.1 R2

Chaves sugeridas:

```text
runs/{run_id}/creators/{creator_id}/voice-candidates/{design_hash}/{candidate_id}.mp3
runs/{run_id}/creators/{creator_id}/voice-selected/{voice_ref}.mp3
runs/{run_id}/items/{item_id}/voiceover.mp3
```

Regras:

- checkpoint e banco guardam somente `r2://`;
- signed URLs são derivadas na API/SSE;
- base64 e URLs temporárias do provider nunca entram no estado;
- previews não selecionados seguem a política normal de retenção;
- `down` e `reset` local não apagam R2.

## 11. Idempotência, retry e falhas

Voice Design e criação de voz são efeitos pagos e potencialmente não idempotentes.

Usar `external_effects` com chaves:

```text
voice-design:{run_id}:{creator_id}:{description_hash}:{reroll_count}
voice-finalize:{run_id}:{creator_id}:{candidate_id}
voiceover:{run_id}:{item_id}:{script_hash}:{voice_ref}
```

Política:

- reservar efeito antes da chamada;
- replay de efeito `succeeded` devolve o resultado armazenado;
- erro de conexão anterior ao envio pode ser retentado;
- 429 explícito pode usar backoff limitado;
- read timeout depois do envio vira `uncertain`, sem retry cego;
- output nulo, vazio ou malformado falha imediatamente com erro do adapter;
- criação incerta tenta reconciliar pelo nome determinístico antes de nova chamada;
- nunca coagir `None` para string;
- falha de Voice Design mantém a imagem;
- falha de finalização impede fan-out e nunca produz final silencioso.

Segredos, descrição integral, imagem, oferta e roteiro não entram em logs/traces.
Tracing pode expor:

- provider/model;
- `prompt_version`;
- `description_hash`;
- contagem de caracteres;
- request/trace ID do provider;
- status, duração e custo.

## 12. Configuração

Mover toda configuração não secreta para `config/pipeline.yaml`:

```yaml
voice:
  mode: designed
  provider: elevenlabs
  design_model: eleven_ttv_v3
  tts_model: eleven_turbo_v2_5
  language_code: pt-BR
  candidates_per_creator: 3
  max_rerolls_per_creator: 2
  selection_without_review: first
  prompt_version: voice-match-v1
  preview_text_version: pt-br-ugc-v1
  request_timeout_seconds: 120
  concurrency: 1
```

Provider live:

```yaml
adapters:
  creator: creator_vercel_elevenlabs_design
```

Remover de `.env.example` e do preflight:

```dotenv
REPLICATE_ELEVENLABS_MODEL=
REPLICATE_ELEVENLABS_TEXT_FIELD=
REPLICATE_ELEVENLABS_VOICE_FIELD=
REPLICATE_ELEVENLABS_VOICE_ID=
REPLICATE_ELEVENLABS_VOICE_ID_FEMALE=
REPLICATE_ELEVENLABS_VOICE_ID_MALE=
REPLICATE_ELEVENLABS_VOICE_ID_NEUTRAL=
```

Adicionar ao preflight live:

```dotenv
ELEVENLABS_API_KEY=
```

`/readyz` deve exigir a chave somente quando o adapter direto estiver selecionado.
O preflight nunca imprime o valor.

## 13. Custo e quotas

Voice Design gera três previews em uma chamada e cobra pelo texto usado no
preview. A aplicação deve:

- usar um texto fixo versionado de 100–200 caracteres;
- limitar a três candidatos;
- permitir no máximo dois rerolls por creator;
- não salvar os três candidatos na biblioteca de vozes;
- criar um voice slot somente para o candidato aprovado;
- registrar todas as tentativas, inclusive descartadas;
- somar `voice_design` e `voiceover` separadamente em `cost_by_stage`;
- preferir custo real de headers/resposta e usar estimativa configurada somente
  quando o provider não informar.

O resumo do run deve cumprir:

```text
total_cost_usd =
  creative_generation_cost
  + voice_design_cost
  + video_cost
  + voiceover_cost
  + assembly_cost
```

Adicionar proteção operacional para:

- limite de Voice Design por run;
- limite de voice slots ativos;
- alerta de custo anômalo;
- limpeza explícita de vozes órfãs criadas, mas nunca associadas a creator
  aprovado.

## 14. Segurança e privacidade

O matcher visual não pode inferir ou registrar:

- etnia;
- condição de saúde;
- deficiência;
- orientação sexual;
- religião;
- nacionalidade;
- idade exata;
- identidade de gênero;
- sotaque a partir do rosto.

Ele pode descrever apenas sinais úteis à direção criativa, como:

- apresentação visual;
- formalidade;
- energia da cena;
- estilo UGC;
- expressão aparente;
- coerência entre `visual_brief` e `performance_style`.

Para imagens enviadas pelo usuário:

- documentar consentimento e finalidade;
- não usar reconhecimento facial ou comparação de identidade;
- não clonar voz sem amostra de áudio e autorização;
- não afirmar que a voz é “a voz real” da pessoa;
- apresentar o resultado como voz sintética coerente com o personagem.

## 15. Estratégia TDD

Seguir RED → GREEN → refactor em cada tranche. Nenhum teste deve ser removido,
afrouxado, marcado `skip` ou `xfail` para acomodar a mudança.

### 15.1 Contratos

- `CreatorVoiceSpec` aceita somente enums/ranges válidos;
- campos server-owned vencem output do modelo;
- atributos proibidos são rejeitados;
- mapper `VoiceProfile` legado mantém creators históricos.

### 15.2 Adapter ElevenLabs

Com `httpx.MockTransport`:

- request de Voice Design correto;
- três previews válidos;
- resposta com um, dois ou três previews;
- base64 inválido;
- `generated_voice_id` ausente;
- create voice válido;
- `voice_id` nulo/vazio;
- TTS válido e metadata de custo;
- 401/422 sem retry;
- 429 com backoff limitado;
- connect timeout retryável;
- read timeout marcado `uncertain`;
- nenhuma mensagem de erro contém API key.

### 15.3 Grafo

- exatamente um interrupt;
- imagem precede Voice Design;
- aprovação finaliza só o candidato selecionado;
- candidato de outro creator é rejeitado;
- reroll de voz preserva imagem e assignments;
- restart da API entre gate e aprovação retoma corretamente;
- replay não cria voz duplicada;
- falha de finalização não inicia vídeo;
- review desabilitado escolhe deterministicamente;
- mocks continuam offline, determinísticos e custo zero.

### 15.4 PostgreSQL/R2

- migração upgrade/downgrade;
- backfill `legacy`;
- isolamento tenant-scoped;
- `voice_spec` round-trip;
- candidates persistidos como `r2://`;
- API/SSE entrega signed URL;
- signed URL nunca é salva;
- R2 sobrevive a `down` e `reset`.

### 15.5 API/frontend

- seleção obrigatória;
- stale 409 e cancelled 410;
- `target=voices`;
- edição invalida preview;
- frontend toca os três candidatos;
- loading e erro por creator;
- aprovação bloqueada sem seleção;
- build TypeScript/Vite verde.

### 15.6 Custo

- Voice Design entra no total antes do fan-out;
- rerolls descartados permanecem contabilizados;
- TTS usa custo real quando disponível;
- assembly FFmpeg continua zero;
- nenhum custo é duplicado após resume.

## 16. Ordem de implementação

### Tranche 0 — spike controlado

1. Validar Voice Design com a conta ElevenLabs real.
2. Confirmar validade/reconciliação de `generated_voice_id`.
3. Confirmar limites de voice slots e endpoint de listagem/remoção.
4. Medir qualidade pt-BR com `eleven_ttv_v3` + `eleven_turbo_v2_5`.
5. Registrar somente resultados técnicos, sem inserir credenciais em docs.

Aceite: uma voz desenhada manualmente, salva e usada em TTS; custo e headers
observados.

### Tranche 1 — contratos e mock

1. Criar contratos Pydantic.
2. Adicionar ports e typed tools.
3. Implementar mock determinístico.
4. Atualizar `config-mock` e `config-staging`.

Aceite: suíte offline completa, sem rede e custo zero.

### Tranche 2 — adapter direto

1. Implementar Voice Design.
2. Implementar finalize/reconcile.
3. Implementar TTS direto.
4. Validar shapes, retries, tracing e custo.

Aceite: testes HTTP isolados com 100% de cobertura nos novos ramos.

### Tranche 3 — grafo e efeitos duráveis

1. Separar imagem e candidatos de voz.
2. Inserir `voice_candidates` e `finalize_voices`.
3. Adicionar `target=voices`.
4. Reservar/reconciliar efeitos.
5. Atualizar resumo de custo.

Aceite: gate/resume/restart comprovados com PostgreSQL.

### Tranche 4 — persistência e API

1. Criar migração `0010`.
2. Atualizar repository/normalização/legacy import.
3. Persistir candidates no R2.
4. Separar contratos HTTP de request/response.
5. Assinar previews somente na saída.

Aceite: round-trip PostgreSQL/R2 e isolamento tenant verdes.

### Tranche 5 — frontend

1. Exibir três previews.
2. Selecionar candidato.
3. Editar `voice_brief`.
4. Reroll apenas da voz.
5. Mostrar custo e erros.

Aceite: fluxo completo do gate sem segundo interrupt.

### Tranche 6 — live e rollout

1. Atualizar live config, `.env.example`, readiness e `scripts/dev-local`.
2. Executar full suite com PostgreSQL 16.
3. Subir `config-staging`, revisar, reiniciar API e retomar.
4. Executar live opt-in com batch 1.
5. Confirmar chamadas externas somente para Vercel, Replicate, ElevenLabs e R2.
6. Tornar `voice.mode=designed` o default live.

Aceite: vídeo final H.264/AAC com a voz aprovada, custo correto e sem pools no
ambiente.

## 17. Rollout e compatibilidade

Durante uma janela de migração:

```yaml
voice:
  mode: pool      # adapter antigo
  # ou
  mode: designed  # adapter novo
```

Regras:

- runs históricos nunca são reescritos;
- creators `legacy` continuam usando seu `voice_ref`;
- novos creators em `designed` não consultam pools do `.env`;
- rollback troca apenas `voice.mode`/adapter, sem downgrade de dados;
- remoção definitiva do adapter de pools ocorre somente depois do live batch 1 e
  de uma campanha retomada após restart.

## 18. Critérios finais de aceite

- Nenhum ID/nome de voz precisa ser configurado no `.env`.
- A voz é derivada de perfil + imagem, com limites de segurança explícitos.
- Idioma/sotaque nunca são inferidos do rosto.
- Três previews aparecem no gate humano já existente.
- O usuário escolhe ou regenera somente a voz.
- Apenas a voz aprovada ocupa um voice slot.
- `voice_ref` é estável e reutilizado na locução final.
- Checkpoint/banco guardam apenas referências canônicas.
- API/SSE assinam mídia somente na saída.
- Restart entre geração, gate e aprovação não duplica custo nem voz.
- O vídeo final tem H.264 + AAC e contém a locução escolhida.
- `cost_by_stage` inclui `voice_design` e `voiceover`.
- `config-mock` permanece offline/determinístico.
- Full suite passa com cobertura obrigatória de 100%.
- Live batch 1 é opt-in e não é disparado automaticamente.

## 19. Arquivos previstos

Principais arquivos a criar/alterar:

```text
src/orchestrator/creative_contracts.py
src/orchestrator/adapters/base.py
src/orchestrator/adapters/elevenlabs_voice_design.py
src/orchestrator/adapters/creator_real.py
src/orchestrator/tools/creators.py
src/orchestrator/tools/registry.py
src/orchestrator/nodes/stages.py
src/orchestrator/graph/builder.py
src/orchestrator/graph/state.py
src/orchestrator/media_store.py
src/orchestrator/registry.py
src/orchestrator/db/models.py
src/orchestrator/db/creators.py
src/orchestrator/web/server.py
migrations/versions/20260729_0010_creator_voice_design.py
front/src/api/contracts.ts
front/src/screens/CampaignDetail.tsx
config/pipeline.yaml
config/providers.yaml
config-mock/*
config-staging/*
.env.example
scripts/dev-local
README.md
docs/DEMO.md
docs/DECISIONS.md
docs/progress/changes/YYYY-MM-DD-slug.md
```

## 20. Referências oficiais

- [ElevenLabs Voice Design quickstart](https://elevenlabs.io/docs/eleven-api/guides/how-to/voices/voice-design)
- [ElevenLabs — Design a voice](https://elevenlabs.io/docs/api-reference/text-to-voice/design)
- [ElevenLabs — Create a voice](https://elevenlabs.io/docs/api-reference/text-to-voice/create)
- [ElevenLabs — Create speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- [Replicate — elevenlabs/turbo-v2.5](https://replicate.com/elevenlabs/turbo-v2.5)
