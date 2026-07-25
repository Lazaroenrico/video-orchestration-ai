"""Operações administrativas que nunca usam a conexão runtime."""
from __future__ import annotations

from psycopg import Connection, sql


RUNTIME_ROLE = "orchestrator_runtime"


def provision_runtime_role(database_url: str, password: str) -> None:
    """Cria ou endurece o papel fixo usado pela API e pelos runners."""
    if not password:
        raise ValueError("ORCHESTRATOR_RUNTIME_PASSWORD é obrigatória")

    with Connection.connect(database_url) as connection:
        role = sql.Identifier(RUNTIME_ROLE)
        connection.execute(
            sql.SQL(
                "DO $$ BEGIN CREATE ROLE {} LOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
            ).format(role, sql.Literal(password)),
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(connection.info.dbname),
                role,
            )
        )
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON ALL TABLES IN SCHEMA public TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                role
            )
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(role)
        )
