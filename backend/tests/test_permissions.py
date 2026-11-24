from uuid import uuid4

import psycopg
import pytest
from conftest import Project
from test_kt3_fixtures import CLIENT_A, SAME_ORDER, create_run, initialize

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
    "SET session_replication_role = replica",
]

DOMAIN_FORBIDDEN = [
    "SELECT * FROM qa.las_fixture_runs",
    "SELECT * FROM qa.las_run_clients",
    "INSERT INTO qa.las_orders SELECT * FROM qa.las_orders",
    "UPDATE qa.las_orders SET status='ACTIVE'",
    "DELETE FROM qa.las_orders",
    "TRUNCATE qa.las_orders",
    "INSERT INTO qa.las_delivery_dates SELECT * FROM qa.las_delivery_dates",
    "UPDATE qa.las_delivery_dates SET delivery_date='2026-11-26'",
    "DELETE FROM qa.las_delivery_dates",
    "ALTER TABLE qa.las_orders ADD COLUMN api_denied integer",
    "UPDATE qa.las_cases SET status='OPEN'",
    "DELETE FROM qa.las_cases",
    "TRUNCATE qa.las_cases, qa.las_idempotency_keys",
    "ALTER TABLE qa.las_cases ADD COLUMN api_denied integer",
    "UPDATE qa.las_idempotency_keys SET request_key='denied'",
    "DELETE FROM qa.las_idempotency_keys",
    "TRUNCATE qa.las_idempotency_keys",
]


def assert_las_privileges(project: Project) -> None:
    run_id, case_id = create_run(project), uuid4()
    assert initialize(project, run_id)
    with project.db("api") as api:
        api.execute("SELECT * FROM qa.las_orders")
        api.execute("SELECT * FROM qa.las_delivery_dates")
        api.execute(
            "INSERT INTO qa.las_cases VALUES (%s,%s,%s,%s,'2026-11-27','DELIVERY_RESCHEDULE','OPEN')",
            (run_id, CLIENT_A, case_id, SAME_ORDER),
        )
        api.execute(
            "INSERT INTO qa.las_idempotency_keys VALUES (%s,%s,'DELIVERY_RESCHEDULE','acl-key',%s,'2026-11-27',%s)",
            (run_id, CLIENT_A, SAME_ORDER, case_id),
        )
        assert api.execute(
            "SELECT case_id FROM qa.las_cases WHERE case_id=%s", (case_id,)
        ).fetchone() == (case_id,)
        assert api.execute(
            "SELECT case_id FROM qa.las_idempotency_keys WHERE case_id=%s", (case_id,)
        ).fetchone() == (case_id,)
        for sql in DOMAIN_FORBIDDEN:
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege) as denied,
                api.transaction(),
            ):
                api.execute(sql)
            assert denied.value.sqlstate == "42501", sql
    with project.db("migrate") as migrator:
        for sql in (
            "CREATE SCHEMA migrant_denied",
            "CREATE TABLE public.migrant_denied (id int)",
            "CREATE TEMP TABLE migrant_denied (id int)",
            "SET ROLE qa_api",
        ):
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege) as denied,
                migrator.transaction(),
            ):
                migrator.execute(sql)
            assert denied.value.sqlstate == "42501", sql
        with migrator.transaction(force_rollback=True):
            migrator.execute("CREATE TABLE qa.test_migrator_owned (id int)")
            migrator.execute("GRANT SELECT ON qa.test_migrator_owned TO qa_api")
            assert migrator.execute(
                "SELECT has_table_privilege('qa_api','qa.test_migrator_owned','SELECT')"
            ).fetchone() == (True,)


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
    assert_las_privileges(migrated)


def test_api_grants_after_repeat(migrated: Project) -> None:
    migrated.compose("stop", "api")
    migrated.start("--update")
    assert_api_privileges(migrated)
    assert_las_privileges(migrated)


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
