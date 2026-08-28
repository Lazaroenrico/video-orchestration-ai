# UGC Orchestrator Resources

## Knowledge

- [README.md](README.md)
  Visao operacional do projeto, comandos, perfil mock e perfil live/hibrido atual. Use para: entender como rodar a pipeline.
- [Context.md](Context.md)
  Fonte de produto original da pipeline de AI UGC em escala. Use para: entender o "por que" de cada stage mantido no motor.
- [docs/DECISIONS.md](docs/DECISIONS.md)
  ADR leve do projeto, com decisoes de arquitetura e tradeoffs. Use para: saber por que o grafo, adapters, gateway, retry e UI foram desenhados assim.
- [docs/PROGRESS.md](docs/PROGRESS.md)
  Painel vivo do estado atual, bloqueios e dez entregas recentes. Use para: encontrar o handoff atual e navegar ao histórico detalhado.
- [src/orchestrator/graph/builder.py](src/orchestrator/graph/builder.py)
  Topologia LangGraph: grafo de topo, subgrafo por item, fan-out e gates. Use para: localizar o fluxo real.
- [src/orchestrator/adapters/base.py](src/orchestrator/adapters/base.py)
  Contratos dos ports e `VoiceProfile`. Use para: entender como providers reais e mock precisam se comportar.
- [src/orchestrator/language_runtime.py](src/orchestrator/language_runtime.py)
  `LanguageRuntime` para resolução de modelos de linguagem e execução de agentes criativos nativos LangChain (D46).
- [src/orchestrator/registry.py](src/orchestrator/registry.py)
  `CompositeAdapter` (domain/media-only) e role routing de mídia/efeitos por YAML. Use para: entender como trocar providers sem mexer no grafo.
- [src/orchestrator/nodes/stages.py](src/orchestrator/nodes/stages.py)
  Os stages como nodes e a logica de roster, approval, video, QC, assembly e feedback. Use para: diagnosticar comportamento do run.
- [src/orchestrator/adapters/openai_image.py](src/orchestrator/adapters/openai_image.py)
  Prompt seguro de imagem e GPT Image 2 via Vercel Gateway. Use para: ajustar aparencia do creator.
- [src/orchestrator/adapters/creator_real.py](src/orchestrator/adapters/creator_real.py)
  Composicao imagem + Voice Design e reroll de voz. Use para: entender o nascimento do creator real.
- [src/orchestrator/adapters/elevenlabs_voice_design.py](src/orchestrator/adapters/elevenlabs_voice_design.py)
  ElevenLabs Voice Design direto (`eleven_ttv_v3`), previews e finalização determinística.
- [src/orchestrator/adapters/_throttle.py](src/orchestrator/adapters/_throttle.py)
  Rate limiter global do Replicate. Use para: entender e ajustar 429/rate limit.

## Wisdom (Communities)

- Equipe/proprietario deste repo
  Melhor fonte para validar comportamento desejado de produto, principalmente quando uma falha pode ser bug de negocio ou limitacao de provider.

## Gaps

- Falta uma referencia externa fixada para os contratos atuais dos modelos Replicate usados em producao.
- Falta documentar um playbook operacional de env vars reais por ambiente.
