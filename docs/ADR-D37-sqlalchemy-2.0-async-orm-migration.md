# ADR-D37 — Migração para SQLAlchemy 2.0 Async ORM na Camada de Dados PostgreSQL

- **Status:** Aprovado e Implementado
- **Data:** 2026-07-28
- **Autor:** Equipe de Arquitetura / UGC Orchestrator

---

## Contexto

Na Fase 2 (ADR-D36), a fundação PostgreSQL com Row Level Security (RLS) multi-tenant foi introduzida para substituir a persistência SQLite/JSON em memória por banco relacional de produção. No entanto, as consultas SQL eram construídas via *strings nativas inline* (`SELECT ... FROM ... WHERE ...`) espalhadas pelos repositórios (`admin.py`, `prompts.py`, `creators.py`, `feedback.py`, `artifacts.py`, `runs.py`, `effects.py`, `jobs.py`).

Essa abordagem apresentava limitações significativas:
1. **Falta de verificação de tipos em tempo de compilação/análise estática** para campos e colunas do schema.
2. **Hardcoded SQL** dificultando refatorações e manutenção do schema.
3. **Risco de inconsistências sintáticas** na montagem manual de cláusulas `ON CONFLICT` e `WITH ... FOR UPDATE SKIP LOCKED`.

---

## Decisão

Refatorar toda a camada de acesso a dados em `src/orchestrator/db/` para utilizar **SQLAlchemy 2.0 Async ORM Declarativo** e seleções/expressões tipadas.

### 1. Mapeamento Declarativo dos Modelos (`src/orchestrator/db/models.py`)
Criamos 17 classes herdeiras de `DeclarativeBase` cobrindo 100% das tabelas do schema PostgreSQL:
- **Tenant & RLS:** `Organization`, `User`, `OrganizationMember`
- **Prompts:** `PromptTemplate`, `PromptLastUsed`
- **Creators:** `Creator`
- **Feedback:** `RunFeedback`
- **Artifacts:** `Artifact`
- **Runs & Projeção:** `Run`, `RunItem`
- **Fila Durável & Audit:** `Job`, `RunGate`, `RunEvent`, `Outbox`
- **Cotas & Efeitos:** `ProviderQuota`, `EffectLedger`
- **Migrações Legadas:** `LegacyImportBatch`, `LegacyImportEntry`

### 2. Substituição de SQL Inline por Expressões Tipadas
- Consultas `SELECT`: Expressas via `select(Model)`.
- Inserções Idempotentes: Expressas via `pg_insert(Model).on_conflict_do_update()`.
- Exclusões/Atualizações: Expressas via `delete(Model)` e `update(Model)`.
- Claim de Fila: Expressa via `select(...).with_for_update(skip_locked=True)`.

### 3. Compilador Transparente e Resolução Post-Compile (`Database.execute`)
Introduzimos o assistente `Database.execute(connection, statement)` em `src/orchestrator/db/database.py`:
- Compila declarações do SQLAlchemy 2.0 especificamente para o dialecto `postgresql`.
- Realiza a serialização automática de campos JSONB (dicionários e listas Python) para JSON via `json.dumps()`.
- Resolve expansões post-compile para cláusulas dinâmicas como `IN (...)` e `NOT IN (...)`.

---

## Consequências e Resultados

1. **Eliminação do Hardcode SQL:** Nenhuma string SQL bruta permanece nos repositórios PostgreSQL do projeto.
2. **Manutenção de Contratos:** Nenhuma assinatura de repositório ou interface externa HTTP/CLI foi alterada.
3. **Isolamento Tenant & RLS:** Todas as chamadas continuam sendo executadas dentro do gerenciador de contexto `Database.connection(tenant)`, preservando o escopo de variáveis da sessão PostgreSQL (`app.organization_id` e `app.user_id`).
4. **Validação:** A suíte completa de **1093 testes passou 100% verde**, e a execução E2E da pipeline mock foi validada com sucesso.
