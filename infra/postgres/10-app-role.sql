-- Papel usado por API/Runner/migrations locais. O POSTGRES_USER fica só no bootstrap.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orchestrator') THEN
        CREATE ROLE orchestrator
            LOGIN
            PASSWORD 'orchestrator'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOBYPASSRLS;
    ELSE
        ALTER ROLE orchestrator NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

ALTER DATABASE orchestrator OWNER TO orchestrator;
\connect orchestrator
ALTER SCHEMA public OWNER TO orchestrator;
GRANT ALL ON SCHEMA public TO orchestrator;
