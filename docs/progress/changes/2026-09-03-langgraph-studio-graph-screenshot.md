# Captura do grafo no LangGraph Studio

Data: `2026-09-03`

## Resultado

O README agora apresenta uma captura legível do grafo executável aberto no LangGraph
Studio, do node `__start__` ao `__end__`, com o gate de revisão e o subgrafo por item.

## Mudanças de contrato

Nenhuma. A alteração é exclusivamente documental.

## RED → GREEN

- **RED:** o README descrevia a topologia em texto e Mermaid, mas não continha uma
  captura real do grafo carregado no LangGraph Studio.
- **GREEN:** a captura validada foi adicionada em `docs/assets/langgraph-studio.png` e
  referenciada na seção de arquitetura do README.
- **REFACTOR:** o formulário de entrada do Studio foi recolhido e a topologia ajustada
  à área visível para eliminar cortes e manter todos os nodes legíveis.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| A primeira captura cortava os nodes superiores e inferiores. | O formulário de entrada ocupava parte do canvas e o grafo não estava ajustado à área disponível. | O formulário foi recolhido e o controle `Fit graph to view` foi acionado antes da captura final. |

## Verificação final

- PNG validado visualmente com todos os nodes, conexões e loops visíveis.
- Arquivo confirmado como PNG RGB de `1200 × 1400` pixels.
- Referência Markdown do README validada contra o caminho do asset.

## Pendências ou bloqueios externos

Nenhum.
