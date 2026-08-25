# Quebra de ciclos de camadas: artifacts db↔storage e vazamentos de adapters

Data: `2026-08-25`

## Resultado

O pacote `orchestrator.db` deixou de conhecer `orchestrator.storage.db` e o grafo:
`ArtifactRecord` migrou para o módulo neutro `orchestrator/storage/records.py`
(importado pelos dois lados), e a subida do schema do checkpointer saiu das
migrações SQL para o comando `orchestrator migrate` (composition root). No eixo
de adapters, `CompositeAdapter` ganhou accessors explícitos (`is_mock`,
`has_role`, `get_role`) e a detecção de adapter pago em `tools/base.py` parou de
farejar nomes de classe; constantes do provider ElevenLabs chegam às tools via
registry; o fallback mock do `ReplicateVideoAdapter` passou a ser injetado pela
composition root (o módulo pago não importa mais MockAdapter); e
`terminal_submission` é símbolo público em `adapters/mock.py`. Comportamento
público de adapters/tools preservado — só a fiação interna mudou.

## Mudanças de contrato

- **Novo módulo neutro** `orchestrator.storage.records`: home canônica de
  `ArtifactRecord`. `orchestrator.storage.db.ArtifactRecord` continua existindo
  como re-export (mesmo objeto), então todos os importadores atuais
  (`media_store`, `legacy_import`, testes) seguem funcionando sem alteração.
- **`upgrade_database(database_url, revision="head")` não sobe mais as tabelas do
  checkpointer** (antes, implicitamente quando `revision == "head"`). Quem precisa
  do par completo chama também `setup_postgres_checkpointer(url)` — a CLI `migrate`
  já faz isso. Ver "Pendências" para o impacto nos testes PostgreSQL.
- **`open_artifact_repository(path, *, postgres_factory=None)`**: novo parâmetro
  keyword-only de inversão. O default preserva o comportamento histórico
  (import tardio de `PostgresArtifactRepository`).
- **`ReplicateVideoAdapter(..., mock_clip_generator=None)`**: política de fallback
  injetada. `allow_mock_fallback=True` sem gerador injetado levanta o mesmo
  `RuntimeError("...mock fallback disabled...")` do fallback desligado. O registry
  (`_build_replicate`) sempre injeta o gerador mock — adapters construídos via
  providers.yaml mantêm comportamento idêntico.
- **`MockAdapter.is_mock = True`** e **`CompositeAdapter.is_mock`** (True somente
  quando todos os papéis são mock), `has_role(role)`, `get_role(role)`.
- **`is_paid_creator_adapter`** decide por `is_mock`/`get_role`; o alias
  `direct_creator_image_enabled` permanece apontando para ela.
- **Público:** `orchestrator.adapters.mock.terminal_submission` (alias privado
  `_terminal_submission` mantido por compatibilidade).
- Nenhuma alteração em schemas, rotas de API ou YAMLs.

Decisões de arquitetura continuam canônicas em `docs/DECISIONS.md`.

## RED → GREEN

Suíte nova: `tests/test_layering_adapters.py` (guardas AST/texto + comportamento).

1. **Ciclo db↔storage**
   - **RED:** `test_artifact_record_has_neutral_home_shared_by_both_sides`
     (`ImportError`/identidade entre módulos),
     `test_db_artifacts_module_does_not_import_storage_db` (AST achou
     `orchestrator.storage.db` em `db/artifacts.py`),
     `test_open_artifact_repository_accepts_injected_postgres_factory`
     (`TypeError: postgres_factory inesperado`).
   - **GREEN:** `storage/records.py` criado com `ArtifactRecord` verbatim;
     `db/artifacts.py` importa de `storage.records` + `storage.retention`;
     `open_artifact_repository` aceita `postgres_factory` injetado.
2. **Migrações sem grafo**
   - **RED:** `test_db_migrations_module_does_not_know_graph` (fonte continha
     "graph"/"checkpoint") e `test_cli_migrate_wires_postgres_checkpointer_after_upgrade`.
   - **GREEN:** bloco removido de `db/migrations.py`; `cli.py` importa
     `setup_postgres_checkpointer` e chama após `upgrade_database`.
3. **Accessors do composite**
   - **RED:** `test_mock_adapter_exposes_is_mock`, `test_composite_is_mock_true_only_when_every_role_is_mock`,
     `test_composite_role_accessors_return_wired_adapters`, `test_tools_base_stops_sniffing_adapter_class_names`.
   - **GREEN:** `is_mock` no MockAdapter; propriedade/métodos no composite;
     `is_paid_creator_adapter` reescrito com accessors públicos. O `__getattr__`
     foi mantido APENAS para sondas opcionais de capacidade
     (`reroll_creator_voice`, `voice`, `image`, `latentsync_*`): call sites em
     `nodes/stages.py`, `tools/creators.py` e `tools/video.py` usam
     `getattr(adapter, ..., None)` e o fallback PRECISA disparar quando o papel
     não expõe a capacidade — método fixo quebraria essa sonda. Documentado no
     próprio código.
4. **Constantes ElevenLabs via registry**
   - **RED:** guardas AST de `tools/creators.py` + igualdade de constantes.
   - **GREEN:** `registry` re-exporta `DEFAULT_PREVIEW_TEXT`/`voice_description_hash`;
     `tools/creators.py` importa delas.
5. **Fallback mock injetado no vídeo**
   - **RED:** guarda de import (`orchestrator.adapters.mock` ausente),
     comportamento com gerador injetado e recusa sem gerador.
   - **GREEN:** `mock_clip_generator` no construtor; `_build_replicate` injeta
     `MockAdapter.generate_clip` via `_mock_clip_generator`. Em
     `tests/test_replicate_video.py`, apenas o helper `_make_adapter` passou a
     injetar o gerador (mesma fiação do registry); **asserções intactas**.
6. **Submissão terminal pública**
   - **RED:** símbolo público inexistente e fonte de `language_runtime.py`
     referenciando o nome privado.
   - **GREEN:** renomeado para `terminal_submission` com alias de compatibilidade;
     `language_runtime.py` usa o nome público.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Teste do CLI migrate recebeu URL Neon do `.env` local em vez do valor do teste. | A opção `--migration-database-url` tem `envvar="MIGRATION_DATABASE_URL"`, presente no `.env` do dev; `load_dotenv` carrega-a durante a invocação e ela vence o `DATABASE_URL`. | Teste fixa também `MIGRATION_DATABASE_URL=""` no ambiente isolado do `CliRunner` (hermeticidade). |
| Guarda `"_by_role" not in source` falhou depois do GREEN. | A própria docstring nova de `is_paid_creator_adapter` citava `` `_by_role` `` literalmente. | Docstring reescrita ("estado interno do composite") sem enfraquecer a guarda. |
| `ValidationError` em `CreatorVoiceSpec` no teste do registry. | Literais errados no teste (`female`/`young-adult`). | Literais canônicos (`feminine`/`young_adult`). |
| Risco de regressão em CI: `tests/test_postgres_checkpoint.py` consulta a tabela `checkpoints` logo após `upgrade_database`, que não sobe mais o schema do checkpointer. | O call de setup era implícito dentro de `upgrade_database`; com o movimento para a CLI, o bootstrap virou contrato explícito. | Fixtures do arquivo passaram a chamar `setup_postgres_checkpointer(url)` após `upgrade_database(url)`, espelhando exatamente o procedimento do comando `orchestrator migrate` (asserções intactas). Demais arquivos de integração PG não consultam tabelas de checkpoint e ficaram intocados. |

## Verificação final

- `rtk proxy python -m pytest <20 suítes afetadas> --no-cov -p no:cacheprovider`:
  **379 passed** (inclui `test_layering_adapters.py` com 20 testes,
  registry/composite, replicate_video, paid effects, tools, artifact/retention,
  media_store, runtime_mode, cli, voice/elevenlabs, live-config).
- Batches complementares verdes: graph/creator/storage-flow (86), contracts/
  state/primitives (106), stages/tracing/judge-cassette (78 passed, 1 skipped —
  skip legítimo opt-in `--live`).
- `ruff check` limpo em todos os arquivos tocados; `ruff format --check` ok.
- Correção pós-verificação (coordenação): `tests/test_postgres_checkpoint.py`
  atualizado conforme a falha investigada acima; validado por coleta limpa
  (`--collect-only`) e `ruff check` no arquivo. Suíte completa pós-integração das
  três ondas: zero FAILED; apenas os 116 erros pré-existentes de infraestrutura
  PostgreSQL (sem servidor em 127.0.0.1:5432).
- Não executados aqui (limitação de infra, sem servidor em 127.0.0.1:5432):
  `tests/test_postgres_*`, `test_operations`, `test_storage_migration`,
  `test_legacy_import`.

## Pendências ou bloqueios externos

- **Executar a suíte PostgreSQL em CI/servidor real** para confirmar as suítes de
  integração após a troca do bootstrap explícito do checkpointer (local sem
  servidor em 127.0.0.1:5432).
- **Aresta de ciclo mantida de propósito:** o fallback default de
  `open_artifact_repository` ainda importa `PostgresArtifactRepository`
  tardiamente dentro de `storage/db.py`. A costura de inversão existe
  (`postgres_factory`), mas o default não foi trocado porque
  `tests/test_runtime_mode.py` trava o comportamento default (e o chamador
  natural — API web — está fora do escopo deste agente). Idem para
  `runtime_mode.open_repository_backend`, cujo import tardio de
  `orchestrator.db` é pré-existente e é o seletor sancionado.
- `tools/video.py` (~linha 400) ainda alcança `ReplicateVideoAdapter._coerce_output`
  (privado) num caminho de LatentSync — vazamento análogo, fora do escopo dos seis
  itens desta entrega; candidato ao próximo refactor de camadas.
