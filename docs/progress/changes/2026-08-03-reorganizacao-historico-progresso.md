# Reorganização do histórico de progresso

Data: `2026-08-03`

## Resultado

`docs/PROGRESS.md` virou um painel curto. As 3.254 linhas do histórico anterior foram
preservadas integralmente nos arquivos mensais de junho, julho e agosto, com manifesto,
inventário dos 76 títulos e checksums de imutabilidade.

## Mudanças de contrato

- Novas entregas usam uma página `docs/progress/changes/YYYY-MM-DD-slug.md` baseada em
  `docs/progress/CHANGE-TEMPLATE.md`.
- O painel mantém somente estado atual, bloqueios acionáveis, dez entregas recentes e o
  índice do histórico.
- Falhas investigadas ficam na página da mudança; `AGENTS.md` e `CLAUDE.md` não exigem
  mais que o relato detalhado seja anexado ao painel.
- Decisões arquiteturais e procedimentos operacionais continuam em seus documentos
  canônicos; a página de mudança registra apenas resultado e link.

## RED → GREEN

- **RED:** o teste documental encontrou 3.254 linhas no painel, ausência dos três
  arquivos mensais e ausência do template de mudança.
- **GREEN:** o histórico foi separado por mês sem alteração de conteúdo; painel,
  manifesto, template, página de mudança e links foram criados.
- **REFACTOR:** checksums dos três arquivos mensais passaram a ser validados pelo teste
  documental, protegendo a regra de imutabilidade do legado.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| `python -m pytest` não iniciou. | Não há `python` no `PATH` da sessão; o projeto usa a venv local. | A verificação foi repetida com `.venv/bin/python`. |
| O contrato documental falhou em três testes. | O arquivo ainda era monolítico e não existiam arquivos mensais nem template. | A estrutura aprovada foi criada sem afrouxar as asserções. |
| O teste do inventário encontrou 76 títulos, mas a expectativa dizia 68. | A contagem inicial foi transcrita incorretamente; o manifesto já continha todas as 76 linhas encontradas por `rg`. | Contador, painel e teste foram corrigidos para o inventário real; os arquivos mensais permaneceram intactos. |
| `rtk ruff` não encontrou o executável. | Ruff está instalado somente na `.venv`, não no `PATH` global usado pelo wrapper dedicado. | A checagem foi executada por `rtk proxy .venv/bin/ruff check ...`. |
| Ruff reportou `I001` no teste novo. | O bloco de imports não estava no formato canônico da versão instalada. | O organizador de imports do Ruff foi aplicado; a checagem completa ficou verde. |
| A suíte completa acumulou erros de setup na faixa PostgreSQL e deixou de avançar em 45%. | `postgresql_noproc` exige servidor em `127.0.0.1:5432`, mas não há servidor nem os binários PostgreSQL configurados neste host. | A execução foi interrompida após os timeouts e o primeiro caso dependente foi isolado: dez testes passaram antes de o fixture falhar com `psycopg.OperationalError`; nenhuma asserção foi alterada. |

## Verificação final

- Suíte documental: **5 passed**; ela valida limite do painel, dez links recentes,
  3.254 linhas legadas, inventário, checksums, template e páginas de mudança.
- A mesma suíte verificou arquivos, links Markdown locais e âncoras em toda a
  documentação do projeto.
- Ruff completo e `git diff --check` passaram.
- Os checksums mensais finais coincidem com o manifesto e com as faixas da fonte.
- A suíte completa foi tentada, mas o fixture PostgreSQL externo falhou no setup pela
  ausência de servidor local; o diagnóstico isolado passou dez testes antes dessa falha.

## Pendências ou bloqueios externos

A migração não tem pendência de código. A repetição integral da suíte exige um ambiente
com PostgreSQL em `127.0.0.1:5432`; os bloqueios operacionais do produto permanecem no
painel principal.
