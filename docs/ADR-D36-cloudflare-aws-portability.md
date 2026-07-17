# ADR-D36 - Hospedagem Cloudflare com portabilidade para AWS

Data: 2026-07-16

Status: proposto para implementacao incremental; nenhuma infraestrutura foi provisionada

Relacionadas: D9 (checkpointer SQLite), D22 (dashboard e SSE), D30 (R2 e artifacts), D34 (streaming SSE) e D35 (gates LangGraph).

## Decisao em uma frase

Hospedar a borda e a SPA na Cloudflare, executar a aplicacao Python em imagens OCI portaveis, e tornar PostgreSQL, uma fila abstrata e storage S3 os contratos duraveis. A migracao para AWS passa entao a ser troca de provedores de compute, fila e storage, sem reescrever o grafo LangGraph nem a API de negocio.

## Contexto atual

O projeto ja possui componentes que favorecem a migracao:

- A SPA React/Vite e estatica; pode ser servida por qualquer CDN.
- A API e FastAPI/Python, e o motor LangGraph usa processos async comuns.
- A midia live ja usa `R2MediaStorage` via boto3 e API S3-compatible; a chave canonica e separada da URL assinada (D30).
- Adapters, grafo e configuracao estao separados por Protocols e YAML.

Mas o runtime ainda nao e hospitavel de forma resiliente em mais de uma instancia:

- Checkpoints, creators, prompts e metadata de artifacts usam SQLite ou JSON local.
- `web/server.py` mantem `_runs`, filas SSE, buffer de replay, erros e `Future` de aprovacao em memoria do processo.
- A execucao nasce em `FastAPI.BackgroundTasks`; nao existe job duravel, lease, idempotencia global ou outbox.
- O limite atual de montagem e 900 segundos. Um consumidor de Cloudflare Queues tem no maximo 15 minutos de wall time, logo nao pode executar um run inteiro com seguranca.
- O bridge de montagem Seedance chama Node.js; a imagem de runtime precisa conter Python e Node, ou separar o worker de montagem.

Esses pontos sao os verdadeiros bloqueadores. Colocar o processo atual em um Container sem altera-los apenas moveria o ponto unico de falha para um disco efemero.

## Arquitetura alvo

```text
Browser
  |
  +-- Cloudflare DNS, WAF e Access/OIDC
  |      |
  |      +-- Worker de borda: SPA estatica, validacao/propagacao de identidade,
  |      |   proxy de /api e de SSE. Nao guarda estado de negocio.
  |      |
  |      +-- Container API (FastAPI): comandos, consultas, SSE e URLs assinadas
  |
  +-- PostgreSQL: usuarios, organizacoes, runs, jobs, gates, eventos,
  |                artifacts e checkpoints LangGraph
  +-- Fila: acorda o runner; nao e a fonte da verdade do job
  +-- Container Runner: executa LangGraph e chama providers externos
  +-- R2/S3: bytes de imagem, audio e video
```

Na Cloudflare, o Worker serve assets, aplica a politica de borda e encaminha requisicoes. O Python fica em Containers Linux/amd64, em dois papeis independentes: API e Runner. Containers possuem disco efemero; portanto apenas temporarios de uma chamada podem usar disco local.

O Worker nao deve reimplementar FastAPI, LangGraph, adapters nem o loop de video. Python Workers hoje usam Pyodide e ainda estao em beta. Mesmo com suporte crescente a pacotes, o projeto exige o ecossistema CPython completo, boto3, Pillow, LangGraph, SDKs de providers e o bridge Node. Containers reduzem esse risco e a mesma imagem OCI e executavel no ECS Fargate.

Durable Objects podem ser necessarios internamente para o ciclo de vida de Containers, mas nunca serao a fonte canonica de runs, jobs, usuarios ou eventos. Isso evita introduzir um modelo de estado sem equivalente direto na AWS.

## Contratos de portabilidade

| Necessidade | Contrato da aplicacao | Cloudflare inicial | AWS equivalente |
| --- | --- | --- | --- |
| Borda e SPA | HTTP, OIDC/JWT, assets estaticos | Worker Static Assets + Access/WAF | CloudFront/S3 + WAF + OIDC, ou manter Cloudflare na borda |
| API e runner | Imagens OCI Linux/amd64, env vars e health checks | Containers | ECS Fargate services/tasks |
| Estado transacional | PostgreSQL e migracoes SQL versionadas | PostgreSQL gerenciado externo | RDS PostgreSQL ou Aurora PostgreSQL |
| Checkpoints LangGraph | `AsyncPostgresSaver` | Mesmo PostgreSQL | Mesmo PostgreSQL |
| Jobs | `JobQueue` interno, idempotency key e lease no banco | Cloudflare Queues como sinal de wake-up | SQS como sinal de wake-up |
| Midia | API S3, `storage_key`, signed URL temporaria | R2 | S3 |
| Segredos | nomes de env sem SDK de plataforma no dominio | Workers/Containers secrets | Secrets Manager ou task secrets |
| Observabilidade | logs JSON, `run_id`, `job_id`, `organization_id`, OpenTelemetry/LangSmith | Logs e analytics Cloudflare | CloudWatch/OTel/LangSmith |

O contrato de storage fica limitado a operacoes S3 que ja usamos: `PutObject`, `GetObject`, `HeadObject`, `DeleteObject` e URLs assinadas. R2 implementa a API S3, mas nao todas as operacoes e extensoes da AWS; evitar features especificas de bucket e lifecycle no codigo de dominio preserva a troca de endpoint e credenciais.

## Persistencia e tenancy

PostgreSQL passa a ser a fonte de verdade do produto. Caso AWS seja o destino mais provavel, o banco pode ser criado em RDS PostgreSQL desde o inicio, aceitando a latencia intercloud medida como custo de deixar os dados no destino. Caso isso nao seja aceitavel no lancamento, escolher um PostgreSQL gerenciado externo com backup logico, TLS e caminho testado para RDS; o contrato continua sendo apenas `DATABASE_URL` e SQL padrao.

O esquema inicial deve conter, no minimo:

- `organizations`, `users` e `organization_members` para identidade e autorizacao.
- `runs`, `run_items`, `artifacts`, `creators`, `prompt_templates` e `feedback` como estado de negocio.
- `jobs` com `id`, `run_id`, `kind`, `status`, `attempt`, `idempotency_key`, `lease_until` e `payload_json`.
- `run_gates` para o payload do interrupt, a decisao, autor, data e versao esperada.
- `run_events` com sequencia monotona por run, tipo, payload sanitizado e data.
- `outbox` para publicar sinais de fila somente depois do commit da transacao.

Toda tabela que representa dado de cliente recebe `organization_id`. A API resolve o sujeito OIDC para um usuario local e checa membership no banco; o claim do provedor nao e a unica regra de autorizacao. Row-Level Security no PostgreSQL entra como segunda barreira depois de as consultas sempre carregarem o escopo da organizacao.

Cloudflare Access pode ser a primeira porta de SSO. A origem deve validar o JWT e mapear o `sub` para o usuario local. Essa fronteira fica atras de uma interface OIDC/JWT: na AWS, Cognito ou outro IdP emite um token que alimenta o mesmo mapeamento de usuario e organizacao.

## Jobs, gates e SSE duraveis

O banco e a fonte de verdade; a fila e somente um acelerador de entrega.

1. `POST /api/run` cria `run`, `job` e `outbox` na mesma transacao e retorna o `run_id`.
2. Um publisher envia o sinal da outbox para a fila. Reentregas sao esperadas e inofensivas pela `idempotency_key`.
3. Um Runner recebe o sinal ou faz polling de recuperacao, reivindica o job por lease no PostgreSQL e executa o grafo.
4. Cada mudanca relevante grava `run_events` antes de ser publicada aos clientes.
5. Quando LangGraph retorna um interrupt, o Runner grava `run_gates` e encerra o job. Nao espera em um `Future`.
6. A aprovacao compara versao, registra a decisao e cria um job de resume com `Command(resume=...)`.
7. O endpoint SSE le eventos persistidos a partir de `Last-Event-ID` e depois acompanha novos eventos. Um reconectado nao depende do buffer de um processo.

Cloudflare Queues entrega pelo menos uma vez e limita consumidores a 15 minutos de wall time. Portanto o consumidor apenas inicia ou acorda o Runner; ele nao hospeda a chamada de montagem nem o run completo. SQS Standard sera o adaptador AWS de mesmo modelo. Pagamentos a providers, geracao de video e qualquer efeito externo recebem chave de idempotencia e registro de tentativa antes da chamada.

O limite de concorrencia de Replicate, hoje local ao processo, deve ser substituido por uma quota global: lease por provider/modelo no PostgreSQL ou um adaptador de rate limit independente. Sem isso, escalar runners multiplicaria chamadas pagas.

## Plano de implementacao

### Fase 0 - Congelar os contratos e medir

- Aceitar esta ADR, definir ambiente inicial e regiao de dados.
- Registrar SLOs: duracao p95 do run, recuperacao de gate, perda tolerada de eventos, RPO e RTO.
- Medir tamanho de imagem, memoria dos runs, chamadas simultaneas por provider e latencia Container-PostgreSQL.
- Definir nomes de variaveis comuns: `DATABASE_URL`, `QUEUE_BACKEND`, `STORAGE_BACKEND`, `OIDC_ISSUER`, `OIDC_AUDIENCE` e segredos por provider.

Aceite: nenhum codigo de dominio depende de binding, URL ou SDK exclusivo da Cloudflare.

### Fase 1 - Empacotar sem mudar comportamento

- Criar uma imagem OCI Linux/amd64 reprodutivel com Python 3.12, Node LTS e build da SPA.
- Expor comandos separados para `api`, `runner` e `migrate`; o Runner ainda pode usar o caminho atual nesta fase.
- Adicionar health/readiness endpoints que verificam configuracao sem chamar providers pagos.
- Remover a exigencia de montar `/media` e `/videos` em producao; permitir disco apenas como temporario.
- Criar manifests de desenvolvimento local que sobem imagem, PostgreSQL e um backend S3 compativel.

Aceite: a mesma imagem sobe localmente e tem comandos inequívocos para API e Runner; a SPA nao depende de arquivo servido pelo FastAPI em producao.

### Fase 2 - Migrar dados para PostgreSQL

- Introduzir migracoes SQL e repositorios para runs, creators, prompts, feedback e artifacts.
- Trocar `AsyncSqliteCompatSaver` por `AsyncPostgresSaver` mantendo `thread_id = run_id` e serializer atual.
- Importar dados SQLite/JSON existentes de forma idempotente; manter modo mock local separado para testes offline.
- Criar organizations, users e memberships antes de expor o ambiente a mais de um cliente.
- Adicionar RLS e testes que provem que uma organizacao nao le ou altera dados da outra.

Aceite: reiniciar API ou Runner nao perde checkpoint, creator, prompt, artifact nem contexto de tenant; a retomada LangGraph ocorre a partir do PostgreSQL.

### Fase 3 - Tornar execucao, aprovacao e stream duraveis

- Implementar `jobs`, `outbox`, leases e retry com backoff; o job e idempotente por desenho.
- Substituir `_runs`, `BackgroundTasks` e `Future` por jobs, `run_gates` e `Command(resume=...)` persistidos.
- Persistir eventos com sequencia e entregar SSE com `id:` e suporte a `Last-Event-ID`.
- Fazer Runner recuperar leases expiradas e jobs pendentes apos restart.
- Implementar quota global de provider e DLQ/estado de falha operacional.

Aceite: duplicar mensagem, derrubar o Runner durante um gate e reconectar SSE nao duplica cobranca, nao perde aprovacao e nao perde evento necessario para reidratar a UI.

### Fase 4 - Publicar na Cloudflare

- Publicar a SPA em Worker Static Assets com fallback de SPA; manter `/api/*` e SSE no Worker antes do backend.
- Configurar WAF, rate limiting, Access/OIDC e CORS estrito. O backend valida o JWT recebido.
- Subir API Container e Runner Container como processos distintos, com imagens imutaveis e segredos injetados pela plataforma.
- Ligar Cloudflare Queues ao publisher/launcher do Runner; limitar concorrencia conforme cada provider.
- Manter R2 privado, com CORS restrito e somente URLs assinadas de curta duracao para browser/provider.

Aceite: uma atualizacao rolling nao interrompe runs ja checkpointados; um deploy pode ser repetido e revertido sem migracao manual de estado.

### Fase 5 - Operacao e seguranca

- Centralizar logs estruturados, metricas de fila, falhas por provider, custo por run e traces LangSmith.
- Configurar backup e restore testado de PostgreSQL, inventario de objetos e politica de retencao/purge agendado.
- Criar alertas para lease expirada, DLQ, erro de assinatura, stream atrasado, limite de provider e gasto anomalo.
- Executar testes de carga, recuperacao de desastre e teste de isolamento entre organizacoes.

Aceite: o time consegue reconstruir o estado de um run por `run_id`, recuperar dados dentro de RPO/RTO acordados e provar acesso isolado por organizacao.

### Fase 6 - Exercitar a migracao para AWS

- Publicar a mesma imagem OCI no ECR e subir API/Runner em ECS Fargate, inicialmente sem trafego.
- Apontar para o mesmo PostgreSQL quando ele ja estiver em RDS; caso contrario, executar migracao logica validada para RDS.
- Adicionar adaptador SQS ao contrato de fila; pausar novos jobs Cloudflare, drenar os leases existentes e trocar o publisher.
- Copiar R2 para S3 preservando `storage_key`, checksum e metadata. Durante a transicao, ler os dois backends pelo campo `storage_backend` ja registrado em `artifacts`.
- Trocar a escrita de objetos para S3, validar URLs assinadas e apenas depois mudar DNS/origem. A borda Cloudflare pode permanecer na frente do ECS para reduzir risco.

Aceite: um run iniciado antes da troca continua consultavel, seus artefatos continuam renderizaveis e um novo run em AWS usa os mesmos contratos de banco, fila e storage.

## Regras de implementacao

- Nunca guardar estado de negocio, checkpoint ou evento somente em memoria de Worker, Container ou navegador.
- Nunca usar D1, KV ou Durable Object como banco canonico de runs. Podem ser caches auxiliares, nunca requisito de leitura correta.
- Nunca executar um run completo no callback de fila.
- Nunca persistir URL assinada como URI canonica; manter `storage_key` e assinar na borda de saida.
- Nunca assumir entrega exactly-once. Deduplicar por chave de negocio antes de chamar provider pago.
- Nunca misturar identidade de borda com autorizacao de tenant: JWT identifica; PostgreSQL autoriza.
- Toda dependencia de plataforma precisa ficar em adaptadores de infraestrutura, nao em nodes LangGraph, tools ou adapters de dominio.

## Riscos e gates de decisao

- **Latencia intercloud:** se PostgreSQL estiver em AWS e compute na Cloudflare, medir antes de escalar. Se o p95 prejudicar o SLO, ajustar colocacao/regiao ou adiar a separacao fisica; nao criar cache de estado que quebre consistencia.
- **Containers Cloudflare:** o produto e adequado ao runtime Linux, mas sua camada de controle usa Worker/Durable Object. Manter essa camada fina e sem dominio reduz o custo de trocar por ECS.
- **SSE longo:** o proxy de borda deve preservar streaming e nao bufferizar respostas. Testar reconexao em deploy, rede instavel e URL R2 expirada.
- **Migracao de midia:** R2 e S3 sao compativeis, nao identicos. Validar o subconjunto S3 usado por testes de contrato contra os dois endpoints.
- **Custos de providers:** retries de fila e reinicios sao normais; sem idempotencia e rate limiting global, a migracao aumenta custo em vez de confiabilidade.

## Fora de escopo desta ADR

- Provisionar conta Cloudflare, AWS, banco, filas ou DNS.
- Mudar agora o grafo, os adapters ou o frontend.
- Escolher o IdP comercial definitivo.
- Converter a distribuicao/postagem em novo stage do produto.

## Referencias oficiais consultadas

- [Cloudflare Containers: ciclo de vida e disco efemero](https://developers.cloudflare.com/containers/platform-details/architecture/)
- [Cloudflare Workers: Static Assets para SPA](https://developers.cloudflare.com/workers/static-assets/)
- [Cloudflare Queues: entrega pelo menos uma vez](https://developers.cloudflare.com/queues/reference/delivery-guarantees/)
- [Cloudflare Queues: limites de 15 minutos para consumidores](https://developers.cloudflare.com/queues/platform/limits/)
- [Cloudflare R2: compatibilidade S3](https://developers.cloudflare.com/r2/api/s3/api/)
- [Cloudflare R2: URLs assinadas](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)
- [LangGraph: checkpointer PostgreSQL em producao](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [AWS ECS: task definitions para Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html)
