# ADR-D52: fundações e fronteiras da refatoração estrutural

Data: `2026-08-25`  
Status: aceito e implementado

## Contexto

A configuração por ambiente, os metadados da topologia, a seleção do modo de
execução e o servidor web acumulavam representações paralelas do mesmo contrato.
Além da duplicação, isso permitia que uma alteração válida em uma camada não fosse
refletida nas projeções de progresso, SSE ou persistência. A refatoração precisa
reduzir essas fontes de divergência sem alterar as cinco fases públicas, o gate
humano único, o modo offline determinístico ou os contratos duráveis existentes.

## Decisão

- `config-base/` contém a configuração e os prompts compartilhados. Cada
  `config*` é um overlay; mapas fazem deep-merge, listas são substituídas e o
  perfil tem precedência. Prompts são resolvidos na ordem perfil → base. O
  diretório base faz parte obrigatória do artefato de deploy.
- `orchestrator.graph.topology` é a fonte canônica dos metadados dos nodes. A
  visão padrão preserva os símbolos públicos históricos; uma visão resolvida a
  partir dos tiers efetivos adiciona metadados determinísticos para tiers de
  vídeo configurados. Builder, progresso e projeções web devem consumir a mesma
  visão runtime, mantendo tiers customizados como contrato suportado por D10.
- `orchestrator.runtime_mode` é a costura canônica para selecionar execução local
  ou durável e para o opt-in de adapters pagos. Guardas internas da stack
  PostgreSQL e validações de argumentos explícitos da CLI continuam nos seus
  módulos; leituras diretas ainda não migradas são débito localizado, não uma
  segunda definição do modo.
- `orchestrator.web.server` é o composition root do FastAPI. Eventos, executor,
  registro in-memory e rotas ficam em módulos por responsabilidade. Re-exports
  do composition root preservam a superfície usada pelos consumidores e pelas
  costuras de teste.
- `ArtifactRecord` vive em `orchestrator.storage.records`, módulo neutro entre os
  backends SQLite/PostgreSQL. Migrações de schema não inicializam o checkpointer;
  a CLI compõe explicitamente upgrade e bootstrap do checkpointer. Chamadas boto3
  do R2 continuam fora do event loop sob limite de concorrência; cancelamento do
  chamador não libera o slot enquanto o worker síncrono ainda executa.
- Primitivas sem estado (`to_plain`, WAV mock, status terminais e inferência de
  gênero) têm uma única implementação em `orchestrator.common`. A inferência de
  gênero considera palavras Unicode completas e mantém precedência feminina
  quando há tokens explícitos dos dois grupos.

## Consequências

Adicionar um node ou tier exige manter um único contrato de topologia e validá-lo
contra o grafo construído. Progresso e SSE deixam de conhecer tabelas próprias.
Perfis de configuração ficam menores, mas deploys precisam transportar
`config-base/`. O split web cria módulos adicionais, compensados por fronteiras
mais claras e compatibilidade no composition root. O runtime offline continua sem
rede e sem custo; PostgreSQL e adapters pagos conservam os mesmos opt-ins.

Esta decisão complementa D10, D30, D45, D46, D48 e D51; não substitui seus
contratos de orquestração, persistência, efeitos pagos ou segurança de prompts.
