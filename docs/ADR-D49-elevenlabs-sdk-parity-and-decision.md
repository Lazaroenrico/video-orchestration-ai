# ADR-D49: Caracterização de paridade do SDK ElevenLabs e decisão arquitetural

Data: `2026-08-17`  
Status: aceito (HITL)

## Contexto

A issue `#7` demandou a caracterização comparativa entre a integração REST artesanal atual
(`ElevenLabsVoiceDesignAdapter` e `ElevenLabsVoiceAdapter` sobre `httpx.AsyncClient`) e o SDK
oficial `AsyncElevenLabs`, para fundamentar uma decisão arquitetural explícita (HITL) sobre
migrar incrementalmente para o SDK ou manter o adapter REST existente.

O sistema exige propriedades rígidas de durabilidade:
1. **Controle de transporte e retry:** Diferenciação estrita entre falhas pré-envio (`ConnectTimeout`, `ConnectError`, `PoolTimeout`) e falhas pós-envio (`ReadTimeout`, `WriteError`), garantindo que requisições `POST` pagas nunca sejam retentadas cegamente.
2. **Integração com PostgresEffectLedger:** Idempotência e deduplicação de efeitos externos com chaves determinísticas e quotas isoladas (`elevenlabs_voice_design_chars`, `elevenlabs_voice_slots`, `elevenlabs_tts_chars`).
3. **Reconciliação determinística:** Capacidade de reconciliar criações incertas via `GET /v1/voices` usando o nome canônico `ugc-{org}-{creator}-{hash}`.
4. **Zero live/paid testing:** Testabilidade total offline via `httpx.MockTransport`.

## Matriz de Paridade Executável

| Dimensão / Operação | Implementação REST Atual (`httpx.AsyncClient`) | SDK Oficial (`AsyncElevenLabs`) | Veredito de Paridade |
|---|---|---|---|
| **Voice Design** (`POST /v1/text-to-voice/design`) | Suporta payload `{voice_description, text, model_id}`, validação base64 e decodificação direta para Data URI `data:audio/mpeg;base64,...`. Limita 1 a 3 candidatos. | Método `client.text_to_voice.create_previews(...)` retorna modelos Pydantic equivalentes. | **Paridade Total**. SDK não simplifica o fluxo já validado pelo schema `creative-v2`. |
| **Create / Finalize Voice** (`POST /v1/text-to-voice`) | Cria voz permanente com nome determinístico server-owned `ugc-{org}-{creator}-{hash}` e `FINALIZED_VOICE_DESCRIPTION`. | Método `client.text_to_voice.create_voice_from_preview(...)` envia o mesmo payload. | **Paridade Total**. |
| **TTS / Voiceover** (`POST /v1/text-to-speech/{voice_id}`) | Consome áudio binário direto de `response.content`, codifica em Data URI e calcula custo estimado por caracteres (`cost_source=estimate`). | Método `client.text_to_speech.convert(...)` retorna stream assíncrono (`AsyncIterator[bytes]`), exigindo bufferização manual. | **REST Superior**. Menor overhead para áudios de curta duração (<1000 caracteres). |
| **List Voices & Reconciliação** (`GET /v1/voices`) | `reconcile_voice` consulta `/v1/voices`, filtra pelo nome determinístico e exige unicidade exata (`len(matches) == 1`). | `client.voices.get_all()` lista vozes como lista de objetos `Voice`. | **Paridade Total**. |
| **Retry & Idempotência em POST** | `with_transport_retry` só retenta erros pré-envio e `429` (throttle). Erros 5xx ou pós-envio falham imediatamente para proteger o ledger. | O SDK delega retry ao HTTP client interno sem distinção fina de pré/pós-envio, arriscando repetições acidentais de POST pagos. | **REST Superior**. Essencial para prevenir cobrança duplicada. |
| **Test Doubles & Dependências** | Utiliza `httpx.MockTransport` nativo. 0 dependências adicionais no `pyproject.toml`. | Exige dependência adicional `elevenlabs` e mocks complexos de classes do SDK. | **REST Superior**. |

## Decisão

**Decidimos manter o adapter REST atual (`httpx.AsyncClient`) e NÃO migrar para o SDK `AsyncElevenLabs`.**

### Justificativas:
1. **Segurança de Quota e Faturamento:** O adapter REST garante que requisições `POST` (Voice Design, Finalize, TTS) nunca sofram retries indevidos pós-envio em erros 5xx ou timeouts, protegendo as quotas do `PostgresEffectLedger`.
2. **Pegada de Dependências Mínima:** Evita adicionar a dependência pesada `elevenlabs` e potenciais conflitos de versões transitivas com LangChain/LangGraph.
3. **Confiabilidade e Testabilidade:** O código existente possui 100% de cobertura de testes offline determinísticos (36 testes específicos de Voice Design + 12 testes de paridade novos + testes de efeitos pagos).

## Consequências

- A **Issue #7** é concluída com sucesso com a suíte de caracterização e esta decisão documentada.
- A **Issue #12** (`[P2][AFK] Migrar operações aprovadas para AsyncElevenLabs`) torna-se **não aplicável** e deve ser encerrada sem alterações de código.
- **Rollback:** Caso no futuro o SDK oficial ofereça recursos proprietários críticos (ex.: novos modelos ou websockets), os protocolos `VoicePort` e o encapsulation do adapter permitem plugar o SDK como adapter alternativo sem alterar o grafo de orquestração.
