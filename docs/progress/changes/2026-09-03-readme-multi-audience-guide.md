# README orientado a públicos técnicos e não técnicos

Data: `2026-09-03`

## Resultado

O README foi reorganizado em camadas para recrutadores, pessoas de produto, pessoas
desenvolvedoras e operação. Os snapshots do grafo e da arquitetura aparecem no início,
seguidos por uma leitura simples dos trade-offs; instalação, execução, cotas, testes,
encerramento e diagnóstico foram concentrados no guia operacional ao final.

## Mudanças de contrato

Nenhuma mudança de runtime. A documentação passa a apontar corretamente a API publicada
pelo Docker Compose em `localhost:8005`, mantendo `8000` apenas para execução direta.

## RED → GREEN

- **RED:** o README começava com detalhes de implementação, misturava instalação com
  arquitetura e não explicava de forma operacional a diferença entre cota interna e
  limite comercial do provedor.
- **GREEN:** o documento agora possui navegação por público, visão funcional em cinco
  fases, snapshots antecipados, trade-offs explícitos e três percursos de execução
  (mock, staging e live).
- **REFACTOR:** listas extensas e termos repetidos foram substituídos por tabelas e
  explicações progressivas, preservando os contratos técnicos relevantes.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| O README indicava `localhost:8000` para a API da stack Docker. | `8000` é a porta interna; o Compose publica `8005:8000`. | O guia diferencia a porta Docker `8005` da porta `8000` usada sem containers. |
| Erros como `524/500` e `3/2` pareciam cotas do provedor externo. | Faltava explicar que a mensagem vem do ledger local e representa consumo reservado mais a nova operação. | A seção de cotas documenta fórmula, buckets, defaults e comandos de ajuste sem reset contábil. |
| A primeira execução dos testes não encontrou `pytest`. | O `.venv` existente não continha o extra `dev`. | A validação foi repetida com os extras declarados no `pyproject.toml`. |
| Com apenas o extra `dev`, dois testes da CLI não encontraram `uvicorn`. | Os comandos `api` e `serve` dependem também do extra `web`. | A suíte direcionada foi executada com `--all-extras`, o mesmo ambiente recomendado no README. |

## Verificação final

- `git diff --check` sem erros de whitespace.
- Snapshots referenciados e presentes em `docs/assets/`.
- Links locais do README validados contra arquivos existentes.
- Valores padrão de cotas conferidos em `src/orchestrator/cli.py`.
- Porta pública conferida no contrato automatizado de `docker-compose.yml`.
- `uv run --all-extras python -m pytest --no-cov tests/test_compose_config.py tests/test_cli.py`:
  20 testes passaram; um warning upstream de depreciação do LangSmith.

## Pendências ou bloqueios externos

Nenhum.
