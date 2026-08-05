# Quotas pagas no ambiente local

Data: `2026-08-03`

## Resultado

`scripts/dev-local` ganhou a ação explícita `quotas`, que configura os três limites do
Voice Design/TTS através do serviço Compose `api`. Assim, o comando sempre usa o
PostgreSQL e o tenant locais, sem herdar por engano o `DATABASE_URL` Neon do host.

## Mudanças de contrato

- A interface exige `--design-chars`, `--voice-slots` e `--tts-chars`, nessa ordem, com
  inteiros positivos.
- A ação funciona somente com `ORCH_DEV_CONFIG_DIR=config` e o serviço `api` ativo.
- Todos os argumentos são validados antes da primeira gravação. Os limites são absolutos
  e cumulativos; a ação não os aplica automaticamente durante `up`.
- O procedimento local está documentado no `README.md` e em `docs/DEMO.md`.

## RED → GREEN

- **RED:** a nova invocação caía em `usage` e não emitia comandos Compose.
  **GREEN:** os três buckets passaram a ser configurados dentro de `api`.
- **RED:** zero, negativos e texto eram encaminhados ao CLI. **GREEN:** a validação de
  inteiro positivo ocorre antes do Compose.
- **RED:** staging aceitava quotas pagas. **GREEN:** perfis diferentes de `config` são
  recusados antes de qualquer comando.
- **RED:** stack inativo só falharia durante a primeira gravação. **GREEN:** um probe
  read-only confirma `api` e retorna orientação clara antes de alterar quotas.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| O perfil live local parava com “quota não configurada”. | O wrapper validava credenciais e subia migrations/API, mas não oferecia um caminho local explícito para as quotas tenant-scoped. | A ação `quotas` executa o CLI dentro de `api`, apontando ao banco e tenant locais. |
| Executar o CLI diretamente no host tentou alcançar o Neon. | O host carrega o `DATABASE_URL` de `.env`; somente o Compose sobrescreve a URL com `postgres:5432`. | O wrapper usa `docker compose exec`/`docker-compose exec`, sem conexão de banco pelo host. |

## Verificação final

- `tests/test_dev_local.py`: 16 testes passaram.
- `tests/test_progress_docs.py`: 5 testes passaram.
- Ruff passou nos testes alterados.
- A validação de sintaxe Bash e `git diff --check` passaram.

## Pendências ou bloqueios externos

Nenhum bloqueio de implementação. As quotas não foram aplicadas automaticamente porque
liberam chamadas externas pagas; o operador continua escolhendo explicitamente os limites.
