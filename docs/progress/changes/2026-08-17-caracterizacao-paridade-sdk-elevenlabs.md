# Caracterização de paridade do SDK ElevenLabs e decisão arquitetural

Data: `2026-08-17`

## Resultado

Matriz de paridade executável desenvolvida em `tests/test_elevenlabs_sdk_parity.py` cobrindo
Voice Design, Create/Finalize, TTS, List Voices/Reconcile, erros, timeouts e retries. A decisão
arquitetural HITL (D49) determinou a manutenção da implementação REST atual (`httpx.AsyncClient`)
para preservar o particionamento fino de retries (proteção contra cobrança dupla em POST),
eliminar dependências extras no `pyproject.toml` e manter 100% de testabilidade offline determinística.

## Mudanças de contrato

Nenhuma mudança de contrato público.
Decisão arquitetural canônica registrada em `docs/DECISIONS.md` como `D49 — Caracterização de paridade do SDK ElevenLabs e decisão arquitetural`.

## RED → GREEN

- **RED:** `tests/test_elevenlabs_sdk_parity.py` validando os contratos esperados e falhando por falta de asserções estritas de transporte e parâmetros do schema.
- **GREEN:** Suíte completa com 12 testes offline determinísticos cobrindo todos os cenários da matriz de paridade sem qualquer chamada live ou paga.
- **REFACTOR:** Alinhamento dos testes de caracterização com a política de retry do `_retry.py` (fail-fast em 5xx de POST e retries em erros pré-envio/429).

## Falhas investigadas

| Sintoma | Causa | Correção |
| --- | --- | --- |
| Erro de validação Pydantic no `CreatorVoiceSpec` nos primeiros testes de paridade | Uso de literais não permitidos (`resonant`, `dynamic`) na fixture de teste | Ajuste para literais válidos (`warm`, `energetic`) conforme schema `CreatorVoiceSpec` |
| Falha no teste de retry em 503 no POST de Voice Design | A política `with_transport_retry` propositalmente não retenta 5xx em POST para evitar criação duplicada e cobrança indevida | Teste ajustado para caracterizar o comportamento correto de fail-fast em 5xx de POST e retry em 429 / `ConnectError` |

## Verificação final

- `uv run pytest tests/test_elevenlabs_sdk_parity.py --no-cov`: 12 testes passaram (100% OK).
- `uv run pytest tests/test_elevenlabs_voice_design.py tests/test_creator_voice_contracts.py tests/test_paid_voice_effects.py tests/test_elevenlabs_sdk_parity.py --no-cov`: 66 testes passaram.
- `ruff check src/ tests/`: 0 erros de lint.
- Registro D49 adicionado a `docs/DECISIONS.md`.

## Pendências ou bloqueios externos

Nenhum. A issue `#12` (`[P2][AFK] Migrar operações aprovadas para AsyncElevenLabs`) pode ser concluída como "não aplicável" devido à decisão de manter o REST.
