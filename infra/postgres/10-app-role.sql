-- Papéis PostgreSQL:
-- 1. `orchestrator`: papel migrador/proprietário do banco e schemas locais (LOGIN NOSUPERUSER BYPASSRLS).
-- 2. `orchestrator_runtime`: papel não-proprietário usado exclusivamente por API e Runner (LOGIN NOSUPERUSER NOBYPASSRLS).

DO $$
BEGIN
    -- 1. Cria ou ajusta o papel migrador local 'orchestrator' com BYPASSRLS
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator') THEN
        CREATE ROLE orchestrator
            LOGIN
            PASSWORD 'orchestrator'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            BYPASSRLS;
    ELSE
        ALTER ROLE orchestrator
            LOGIN
            PASSWORD 'orchestrator'
            NOSUPERUSER
            BYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;

    -- 2. Cria ou ajusta o papel de runtime 'orchestrator_runtime' com NOBYPASSRLS
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator_runtime') THEN
        CREATE ROLE orchestrator_runtime
            LOGIN
            PASSWORD 'orchestrator_runtime'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    ELSE
        ALTER ROLE orchestrator_runtime
            LOGIN
            PASSWORD 'orchestrator_runtime'
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION;
    END IF;
END
$$;

-- Ownership do banco para o migrador
ALTER DATABASE orchestrator OWNER TO orchestrator;

\connect orchestrator

-- Ownership do schema public para o migrador
ALTER SCHEMA public OWNER TO orchestrator;
GRANT ALL ON SCHEMA public TO orchestrator;

-- Permissões de conexão e schema para o runtime
GRANT CONNECT ON DATABASE orchestrator TO orchestrator_runtime;
GRANT USAGE ON SCHEMA public TO orchestrator_runtime;

-- Permissões em tabelas, sequences e funções existentes
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO orchestrator_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO orchestrator_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO orchestrator_runtime;

-- Permissões padrão futuras (para objetos criados por 'orchestrator')
ALTER DEFAULT PRIVILEGES FOR ROLE orchestrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orchestrator_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE orchestrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO orchestrator_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE orchestrator IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO orchestrator_runtime;
