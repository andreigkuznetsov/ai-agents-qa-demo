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
    migrated.compose("stop", "api")
    migrated.start("--update")
    assert_api_privileges(migrated)


def test_api_cannot_elevate_or_mutate_schema(migrated: Project) -> None:
    assert_api_privileges(migrated)


@pytest.mark.parametrize(
    "drift_sql, evidence_sql",
    [
        (
            "GRANT UPDATE (version_num) ON qa.alembic_version TO PUBLIC",
            "SELECT has_any_column_privilege('qa_api','qa.alembic_version','UPDATE')",
        ),
        (
            "GRANT SELECT (status) ON qa.runs TO qa_api WITH GRANT OPTION",
            "SELECT has_any_column_privilege('qa_api','qa.runs','SELECT WITH GRANT OPTION')",
        ),
        (
            "CREATE ROLE qa_drift NOLOGIN; GRANT qa_drift TO qa_api WITH INHERIT FALSE, SET TRUE",
            "SELECT pg_has_role('qa_api','qa_drift','SET') AND NOT pg_has_role('qa_api','qa_drift','USAGE')",
        ),
        (
            "GRANT TEMPORARY ON DATABASE qa_demo TO PUBLIC",
            "SELECT has_database_privilege('qa_api',current_database(),'TEMPORARY')",
        ),
        (
            "ALTER TABLE qa.runs OWNER TO qa_admin",
            "SELECT relowner = 'qa_admin'::regrole FROM pg_class WHERE oid = 'qa.runs'::regclass",
        ),
    ],
    ids=[
        "public-column",
        "column-grant-option",
        "set-only-role",
        "public-temp",
        "unexpected-owner",
    ],
)
def test_update_rejects_privilege_drift(
    migrated: Project, drift_sql: str, evidence_sql: str
) -> None:
    migrated.compose("stop", "api")
    with migrated.db() as admin:
        baseline = admin.execute(
            "SELECT version_num FROM qa.alembic_version"
        ).fetchall()
        admin.execute(drift_sql)
        assert admin.execute(evidence_sql).fetchone() == (True,)

    startup = migrated.start("--update", check=False)
    assert startup.returncode != 0
    assert "migration failed" in startup.stdout + startup.stderr
    assert not migrated.compose("ps", "-q", "api")

    with migrated.db() as admin:
        assert (
            admin.execute("SELECT version_num FROM qa.alembic_version").fetchall()
            == baseline
        )
        assert admin.execute(evidence_sql).fetchone() == (True,)
