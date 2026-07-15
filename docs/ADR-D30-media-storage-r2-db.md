# ADR-D30 - R2 + DB relacional para persistencia de midia

Data: 2026-07-14

Status: aceito para documentacao e implementacao incremental futura

ADR resumida relacionada: `docs/DECISIONS.md`, D30.

## Contexto

O motor atual persiste midia em disco local por meio de `media_store.py`. Imagens,
audios e videos retornados por providers podem vir como URLs volateis, entao o runtime
baixa os bytes para `ORCH_MEDIA` ou `ORCH_VIDEOS` e reescreve as URIs para caminhos
serviveis como `/media/{run_id}/...` e `/videos/{run_id}/...`.

Esse comportamento e correto para mock, dry-run, desenvolvimento local, testes offline e
preview da UI. Para producao, porem, o filesystem local nao deve ser a fonte canonica de
midia. O sistema precisa separar:

- bytes grandes de imagem, audio e video;
- estado transacional e auditavel de runs, items, creators, artifacts, custos e retencao;
- URLs temporarias de acesso para UI, download e handoff para providers externos.

A D29 continua valendo: stages de midia (`creator`, `video`, `assembly`, `upscale`)
permanecem adapter-driven ate haver contratos agentic testados. Esta decisao nao muda a
topologia LangGraph nem permite agents bypassarem tools/adapters.

## Decisao

O modelo canonico de producao sera:

`DB relacional -> fonte da verdade de metadata e estado`

`Cloudflare R2 -> armazenamento dos bytes de midia`

O DB relacional comeca SQLite-first para preservar a experiencia offline atual. O R2
sera acessado via API S3-compatible em ambiente live. O R2 nao guarda estado de negocio;
guarda apenas objetos binarios.

O DB deve armazenar, no minimo, os metadados canonicos de cada artifact:

- `id`
- `run_id`
- `item_id` e/ou `creator_id`
- `kind`
- `storage_backend`
- `storage_key`
- `content_type`
- `size_bytes`
- `sha256`
- `source_uri`
- `retention_class`
- `expires_at`
- `meta_json`

`storage_key` e o ponteiro canonico para o objeto. Signed URLs sao derivadas sob demanda
a partir desse ponteiro e nao devem ser persistidas como verdade.

## Abstracao de storage

A persistencia de midia deve evoluir para uma interface continua, mantendo o
comportamento atual como implementacao local:

- `LocalMediaStorage`: usado por mock, dry-run, desenvolvimento e testes. Nao faz rede.
- `R2MediaStorage`: usado em live. Persiste em Cloudflare R2 via S3-compatible API.

A interface minima planejada e:

- `put_bytes(...)`
- `put_from_url(...)`
- `get_signed_url(...)`
- `delete(...)`
- `exists(...)`

LangGraph, nodes, tools e adapters nao devem conhecer detalhes de R2. Eles continuam
operando com `Artifact` e metadados normalizados. A escolha do backend de storage deve
ser configuravel e manter `config-mock` offline, deterministico e sem custo.

## URLs assinadas

URLs assinadas devem ser geradas apenas quando algum consumidor precisa acessar os
bytes:

- preview/player na UI;
- download humano;
- handoff para provider externo que nao consegue ler `/media/...` local;
- recuperacao de referencia de creator para geracao de video ou assembly.

As URLs assinadas devem ter TTL curto e nao devem substituir `storage_key` no DB. Quando
expirarem, o sistema deve gerar outra URL a partir do artifact canonico.

## Politica de retencao

A limpeza de midia deve ser orientada por metadados no DB, nao por varredura cega do
bucket.

Retencao definida:

- Creator assets: manter.
- Clips aprovados: manter.
- Videos finais montados: manter.
- Clips reprovados: short-lived, expiram apos 3 dias.
- Tentativas intermediarias: short-lived, expiram apos 2 dias.

O DB deve marcar artifacts short-lived com `retention_class` e `expires_at`. Um job de
limpeza futuro pode consultar artifacts expirados, deletar os objetos do storage e
marcar a remocao no DB.

## Consequencias

- O DB relacional vira a fonte canonica de artifacts e relacoes.
- O R2 remove dependencia do filesystem local para producao.
- O modo offline continua usando storage local e sem chamadas externas.
- A UI deixa de depender de paths permanentes em live e passa a receber signed URLs
  sob demanda.
- Providers externos recebem URLs assinadas ou data URIs derivados de artifacts
  canonicos, em vez de paths locais.
- A decisao prepara implementacao incremental sem mudar a topologia do grafo, CLI ou
  contratos publicos da web nesta etapa.

## Fora de escopo

- Migrar imediatamente todos os artifacts existentes.
- Trocar SQLite por Postgres nesta etapa.
- Salvar bytes de midia dentro do DB relacional.
- Tornar bucket R2 publico.
- Fazer agents controlarem storage diretamente.
- Mudar o contrato publico de CLI/web como parte desta documentacao.

## Criterios de aceite da implementacao futura

- `config-mock` continua offline, deterministico e sem custo.
- A suite offline continua verde sem credenciais de R2.
- Artifacts live persistem bytes no R2 e metadata no DB relacional.
- Signed URLs sao geradas sob demanda e nao persistidas como valor canonico.
- Clips reprovados recebem expiracao de 3 dias.
- Tentativas intermediarias recebem expiracao de 2 dias.
- Creator assets, clips aprovados e videos finais nao recebem expiracao automatica.
