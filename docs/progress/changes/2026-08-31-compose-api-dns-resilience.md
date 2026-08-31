# Recuperação automática da API após falhas transitórias do Docker DNS

Data: `2026-08-31`

## Resultado

O serviço `api` do ambiente Docker Compose agora reinicia automaticamente após
falhas transitórias de infraestrutura, incluindo indisponibilidade temporária do DNS
interno durante a abertura do pool PostgreSQL.

## Mudanças de contrato

- A configuração efetiva do serviço Compose `api` passa a declarar
  `restart: unless-stopped`.
- A API passa a ser publicada localmente em `8005`, preservando a porta interna `8000`.

## RED → GREEN

- **RED:** `tests/test_compose_config.py` falhou porque o serviço `api` não possuía
  política de reinício na configuração efetiva do Compose.
- **GREEN:** adicionada a política `restart: unless-stopped` ao serviço `api`; o teste
  de contrato passou e `docker compose config` confirmou o valor efetivo.
- **RED:** o teste de contrato confirmou que a API ainda publicava a porta local `8000`,
  já ocupada por outro projeto.
- **GREEN:** o mapeamento foi alterado para `8005:8000` e validado na configuração
  efetiva do Compose.
- **REFACTOR:** não aplicável.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| A API encerrou com `psycopg_pool.PoolTimeout`, seguida de `EAI_AGAIN api` no proxy do frontend. | O DNS interno do Docker não resolveu `postgres` durante toda a janela de inicialização do pool; sem política de reinício, a API permaneceu encerrada. | Configurada recuperação automática da API com `restart: unless-stopped`; o frontend volta a encontrar `api` quando o serviço reinicia. |
| A recriação da API falhou com `Bind for 127.0.0.1:8000 failed: port is already allocated`. | O container externo `study-open-notebook-surrealdb-1` já publica `127.0.0.1:8000`. | O container externo foi preservado e a porta pública desta API foi alterada para `8005`. |

## Verificação final

- `tests/test_compose_config.py`: 1 passed.
- `docker compose config --format json`: `services.api.restart=unless-stopped`.
- `docker compose config --format json`: porta publicada `8005`, porta interna `8000`.
- `GET http://127.0.0.1:8005/api/v2/me`: 200 com sessão local.
- `GET http://127.0.0.1:5173/api/v2/me`: 200 através do proxy do frontend.
- `git diff --check`: sem erros.

## Pendências ou bloqueios externos

Nenhum.
