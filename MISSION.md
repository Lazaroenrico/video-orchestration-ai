# Mission: Dominar o motor de AI UGC deste repo

## Why
Entender como o projeto orquestra a geracao de UGC com IA para conseguir operar, diagnosticar e evoluir o fluxo real de imagem, voz e video sem quebrar a arquitetura. A prioridade pratica e saber onde cada decisao acontece: grafo, adapter, config, UI e persistencia de midia.

## Success looks like
- Explicar o fluxo completo de um run, de conceitos ate feedback.
- Identificar onde imagem, voz, video, QC e distribuicao sao plugados.
- Diagnosticar problemas de rate limit, voz ausente, creator errado ou midia quebrada.
- Evoluir um adapter real mantendo testes offline deterministas.

## Constraints
- Ensino em portugues, direto e conectado ao codigo local.
- Preferir fontes do proprio repo quando o assunto for o estado atual do sistema.
- Manter v1 testavel offline, com custo zero no perfil mock.

## Out of scope
- Trocar a stack principal de LangGraph/LangChain.
- Implementar distribuicao real ou montagem real nesta trilha inicial.
- Aprofundar em APIs externas alem do que o codigo atual usa.
