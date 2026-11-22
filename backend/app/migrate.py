import sys

from alembic import command
from sqlalchemy import Connection, create_engine
from sqlalchemy.pool import NullPool

from app.database import (
    Settings,
    current_revisions,
    expected_revision,
    migration_config,
)

ROLE_BASELINE = """
SELECT
    (SELECT nspowner = 'qa_migrate'::regrole FROM pg_namespace WHERE nspname = 'qa')
    AND (SELECT rolcanlogin AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
         FROM pg_roles WHERE rolname = 'qa_api')
    AND NOT EXISTS (SELECT FROM pg_auth_members WHERE member = 'qa_api'::regrole)
    AND has_database_privilege('qa_api', current_database(), 'CONNECT')
    AND NOT has_database_privilege('qa_api', current_database(), 'CREATE')
    AND NOT has_database_privilege('qa_api', current_database(), 'TEMPORARY')
    AND has_schema_privilege('qa_api', 'qa', 'USAGE')
    AND NOT has_schema_privilege('qa_api', 'qa', 'CREATE')
    AND NOT has_schema_privilege('qa_api', 'public', 'CREATE')
"""

TABLE_OWNERS = """
SELECT count(*), coalesce(bool_and(relowner = 'qa_migrate'::regrole AND relkind = 'r'), true)
FROM pg_class
WHERE relnamespace = 'qa'::regnamespace AND relname IN ('runs', 'alembic_version')
"""

COLUMN_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "REFERENCES"}


def _check_source(connection: Connection, head: str) -> None:
    if connection.exec_driver_sql(ROLE_BASELINE).scalar_one() is not True:
        raise RuntimeError("unexpected PostgreSQL role or schema configuration")
    count, owned = connection.exec_driver_sql(TABLE_OWNERS).one()
    if not owned or count not in (0, 2):
        raise RuntimeError("unexpected PostgreSQL objects or ownership")
    if current_revisions(connection) != ((head,) if count else ()):
        raise RuntimeError("unsupported source revision")


def _execute_grants(connection: Connection) -> None:
    connection.exec_driver_sql("GRANT SELECT, INSERT ON qa.runs TO qa_api")
    connection.exec_driver_sql("GRANT SELECT ON qa.alembic_version TO qa_api")


def _validate_grants(connection: Connection) -> None:
    for table, allowed in (
        ("qa.runs", {"SELECT", "INSERT"}),
        ("qa.alembic_version", {"SELECT"}),
    ):
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        ):
            params = (table, privilege, table, f"{privilege} WITH GRANT OPTION")
            expected = (privilege in allowed, False)
            table_acl = connection.exec_driver_sql(
                "SELECT has_table_privilege('qa_api', %s, %s), has_table_privilege('qa_api', %s, %s)",
                params,
            ).one()
            if tuple(table_acl) != expected:
                raise RuntimeError("unexpected effective table privileges")
            if privilege in COLUMN_PRIVILEGES:
                column_acl = connection.exec_driver_sql(
                    "SELECT has_any_column_privilege('qa_api', %s, %s), has_any_column_privilege('qa_api', %s, %s)",
                    params,
                ).one()
                if tuple(column_acl) != expected:
                    raise RuntimeError("unexpected effective column privileges")


def upgrade() -> None:
    settings = Settings.from_env()
    if not settings.password:
        raise RuntimeError("migration credentials unavailable")
    config = migration_config()
    head = expected_revision()
    engine = create_engine(
        settings.url("qa_migrate"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.connect_timeout,
            "options": "-c lock_timeout=3000 -c statement_timeout=15000",
            "application_name": "qa-migrate",
        },
    )
    try:
        with engine.begin() as connection:
            _check_source(connection, head)
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            _execute_grants(connection)
            if current_revisions(connection) != (head,):
                raise RuntimeError("unexpected target revision")
            _validate_grants(connection)
    finally:
        engine.dispose()


def main() -> int:
    try:
        upgrade()
    except Exception as error:
        print(
            f"migration failed: PostgreSQL configuration, revision or transaction ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    print("migration committed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
