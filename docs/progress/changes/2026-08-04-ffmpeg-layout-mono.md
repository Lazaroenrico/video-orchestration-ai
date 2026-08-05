# Compatibilidade de layout mono na montagem FFmpeg

Data: `2026-08-04`

## Resultado

A montagem final agora declara a saída mono do resampler antes de preencher o fim da
locução com silêncio. O mesmo filtro funciona no FFmpeg 5.1.9 da imagem Bookworm e no
FFmpeg 6.1.1 do ambiente de desenvolvimento, preservando AAC a 48 kHz e os 16 segundos
do vídeo final.

Um smoke comportamental passa a executar o adapter público dentro da imagem OCI após o
build. Uma falha de compatibilidade do FFmpeg bloqueia migração e deploy.

## Mudanças de contrato

Nenhuma mudança em API, schema, banco ou configuração. O contrato interno de mídia da
locução fica explícito como mono a 48 kHz antes da codificação AAC, compatível com a
normalização já definida pela D43.

## RED → GREEN

- **RED:** o smoke com dois clips sintéticos e um MP3 mono, executado pela interface
  pública `FfmpegAssemblyAdapter.assemble` na imagem Bookworm existente, reproduziu
  `Cannot select channel layout` entre `aresample` e `apad`.
- **GREEN:** `aresample` passou a declarar `out_chlayout=mono`; o smoke concluiu na
  imagem reconstruída com FFmpeg 5.1.9 e o teste do adapter continuou verde no FFmpeg
  6.1.1.
- **REFACTOR:** o smoke foi ligado ao job que publica a imagem, cobrindo o binário real
  do runtime em vez de depender somente do FFmpeg instalado no runner da suíte Python.

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| A montagem de `web-936adfeb` falhou em `Parsed_aresample → Parsed_apad` para `stream #2:0`. | O `loudnorm` dinâmico seguido de `aresample=48000` não negociava o layout de saída no FFmpeg 5.1.9, embora o MP3 fosse mono válido. | Declarar a saída mono diretamente no `aresample`. |
| A suíte existente não detectou a incompatibilidade antes do run live. | Os testes usavam FFmpeg 6.1.1 do host, enquanto a imagem publicada usava FFmpeg 5.1.9 do Debian Bookworm. | Executar um smoke de assembly dentro da própria imagem OCI e bloquear o deploy em caso de falha. |
| Duas tentativas da suíte completa pararam em 115 fixtures PostgreSQL e cobertura parcial. | A primeira usou o papel runtime sem `CREATEDB`; a segunda pré-criou `pytest_db`, nome que o janitor precisa criar a partir de seu template. | Usar PostgreSQL 16 isolado com papel administrativo e banco inicial `postgres`; o workflow foi alinhado à mesma topologia. |

## Verificação final

- RED reproduzido na imagem anterior com a mesma mensagem do run live.
- Smoke verde na imagem reconstruída com FFmpeg 5.1.9 e saída H.264/AAC mono a 48 kHz.
- Os 13 testes focados do adapter passaram no FFmpeg 6.1.1.
- Os dois clips e a locução já persistidos de `web-936adfeb` produziram em temporário
  um vídeo H.264/AAC de 16,0 segundos, sem chamadas pagas ou gravações externas.
- Suíte completa: 1.419 testes passaram, 2 testes live foram ignorados como esperado e
  a cobertura permaneceu em 100%.
- Ruff, validação estrutural do workflow, 5 testes documentais e `git diff --check`
  passaram.

## Pendências ou bloqueios externos

Nenhum.
