from uuid import uuid4

import psycopg
import pytest
from conftest import Project

FORBIDDEN = [
    "CREATE SCHEMA api_denied",
    "CREATE TABLE qa.api_denied (id integer)",
    "CREATE TABLE public.api_denied (id integer)",
    "CREATE TEMP TABLE api_denied (id integer)",
    "ALTER TABLE qa.runs ADD COLUMN api_denied integer",
    "DROP TABLE qa.runs",
    "DELETE FROM qa.runs",
    "UPDATE qa.runs SET status = 'CREATED'",
    "UPDATE qa.alembic_version SET version_num = 'denied'",
    "INSERT INTO qa.alembic_version VALUES ('denied')",
    "DELETE FROM qa.alembic_version",
    "SET ROLE qa_migrate",
]


def assert_api_privileges(project: Project) -> None:
    with project.db("api") as api:
        assert (
            api.execute("""
            SELECT current_user = 'qa_api',
                   has_database_privilege(current_user, current_database(), 'CONNECT'),
                   NOT has_database_privilege(current_user, current_database(), 'CREATE'),
                   NOT has_database_privilege(current_user, current_database(), 'TEMPORARY'),
                   has_schema_privilege(current_user, 'qa', 'USAGE'),
                   NOT has_schema_privilege(current_user, 'qa', 'CREATE'),
                   NOT has_schema_privilege(current_user, 'public', 'CREATE'),
                   NOT pg_has_role(current_user, 'qa_migrate', 'SET'),
                   NOT pg_has_role(current_user, 'qa_migrate', 'MEMBER')
        """).fetchone()
            == (True,) * 9
        )
        rev = api.execute("SELECT version_num FROM qa.alembic_version").fetchone()
        uid = uuid4()
        api.execute(
            "INSERT INTO qa.runs (run_id,schema_version,status) VALUES (%s,1,'CREATED')",
            (uid,),
        )
        for command in FORBIDDEN:
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege) as denied,
                api.transaction(),
            ):
                api.execute(command)
            assert denied.value.sqlstate == "42501", command
        assert api.execute(
            "SELECT schema_version,status FROM qa.runs WHERE run_id = %s", (uid,)
        ).fetchone() == (1, "CREATED")
        assert (
            api.execute("SELECT version_num FROM qa.alembic_version").fetchone() == rev
        )


def test_api_grants_after_upgrade(migrated: Project) -> None:
    assert_api_privileges(migrated)


def test_api_grants_after_repeat(migrated: Project) -> None:
    migrated.compose("run", "--rm", "--no-deps", "migrate")
    assert_api_privileges(migrated)


def test_api_cannot_elevate_or_mutate_schema(migrated: Project) -> None:
    assert_api_privileges(migrated)
