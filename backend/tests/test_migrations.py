import importlib
import time
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic import op
from conftest import Project, prepare_kt2
from fastapi.testclient import TestClient
from psycopg import Error


def kt3_catalog(project: Project) -> list[tuple[object, ...]]:
    with project.db() as observer:
        return observer.execute("""
            SELECT c.relname, c.relkind, c.relowner::regrole::text, c.relacl::text,
                   a.attname, format_type(a.atttypid, a.atttypmod), a.attacl::text
            FROM pg_class c
            LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relnamespace = 'qa'::regnamespace
            ORDER BY c.relname, a.attnum
        """).fetchall()


def test_kt3_source_revision_matrix(
    fresh: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
    prepare_kt2(fresh)
    migration = importlib.import_module("app.migrate")
    monkeypatch.setenv("QA_DB_HOST", fresh.host)
    monkeypatch.setenv("QA_DB_PASSWORD", fresh.passwords["migrate"])
    for change, restore in (
        (
            "UPDATE qa.alembic_version SET version_num='unknown'",
            "UPDATE qa.alembic_version SET version_num='0001_runs'",
        ),
        (
            "DELETE FROM qa.alembic_version",
            "INSERT INTO qa.alembic_version VALUES ('0001_runs')",
        ),
        (
            "INSERT INTO qa.alembic_version VALUES ('other_head')",
            "DELETE FROM qa.alembic_version WHERE version_num='other_head'",
        ),
        (
            "ALTER TABLE qa.runs ADD COLUMN test_drift int",
            "ALTER TABLE qa.runs DROP COLUMN test_drift",
        ),
    ):
        with fresh.db() as admin:
            admin.execute(change)
        before = kt3_catalog(fresh)
        with fresh.db() as observer:
            revision = observer.execute(
                "SELECT * FROM qa.alembic_version ORDER BY version_num"
            ).fetchall()
        with pytest.raises(RuntimeError):
            migration.upgrade()
        assert kt3_catalog(fresh) == before
        with fresh.db() as admin:
            assert (
                admin.execute(
                    "SELECT * FROM qa.alembic_version ORDER BY version_num"
                ).fetchall()
                == revision
            )
            admin.execute(restore)


def test_kt3_upgrade_rollback_and_recovery(
    fresh: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
    migration = importlib.import_module("app.migrate")
    monkeypatch.setenv("QA_DB_HOST", fresh.host)
    monkeypatch.setenv("QA_DB_PASSWORD", fresh.passwords["migrate"])
    create = op.create_table
    grants = migration._execute_grants

    def fail_ddl(name: str, *args: Any, **kwargs: Any) -> Any:
        result = create(name, *args, **kwargs)
        if name == "las_fixture_runs":
            op.get_bind().exec_driver_sql("SELECT 1/0")
        return result

    def fail_grant(conn: Any) -> None:
        grants(conn)
        conn.exec_driver_sql("GRANT SELECT ON qa.las_orders TO nonexistent_test_role")

    for source in (None, "0001_runs"):
        row = None
        if source:
            prepare_kt2(fresh)
            with fresh.db("api") as api:
                row = api.execute(
                    "INSERT INTO qa.runs (run_id,schema_version,status) "
                    "VALUES (%s,1,'CREATED') RETURNING *",
                    (uuid4(),),
                ).fetchone()
            with monkeypatch.context() as patch:
                fresh.api_env(patch)
                with TestClient(importlib.import_module("app.main").app) as client:
                    assert client.get("/health/ready").status_code == 503
        before = kt3_catalog(fresh)
        for module, name, error, sqlstate in (
            (op, "create_table", fail_ddl, "22012"),
            (migration, "_execute_grants", fail_grant, "42704"),
        ):
            with monkeypatch.context() as patch:
                patch.setattr(module, name, error)
                with pytest.raises(Exception) as failure:
                    migration.upgrade()
            assert isinstance(failure.value.__cause__, Error)
            assert failure.value.__cause__.sqlstate == sqlstate
            assert kt3_catalog(fresh) == before
            assert not fresh.compose("ps", "-q", "api")
            if source:
                with fresh.db("api") as api:
                    assert api.execute("SELECT * FROM qa.runs").fetchall() == [row]
                    assert api.execute(
                        "SELECT version_num FROM qa.alembic_version"
                    ).fetchone() == (source,)
    volume = fresh.inspect("db")["Mounts"]
    fresh.start("--update")
    assert fresh.inspect("db")["Mounts"] == volume
    assert fresh.health("/health/ready") == (200, b'{"status":"ok"}')
    with fresh.db("api") as api:
        assert api.execute("SELECT * FROM qa.runs").fetchall() == [row]
        assert api.execute("SELECT version_num FROM qa.alembic_version").fetchone() == (
            "0002_las_fixtures",
        )
        assert api.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='qa' "
            "AND table_name='runs' ORDER BY ordinal_position"
        ).fetchall() == [("run_id",), ("schema_version",), ("created_at",), ("status",)]


def test_clean_start(fresh: Project) -> None:
    started = time.monotonic()
    fresh.start()
    assert time.monotonic() - started <= 90.5
    migrate = cast(dict[str, Any], fresh.inspect("migrate")["State"])
    api = cast(dict[str, Any], fresh.inspect("api")["State"])
    assert migrate["ExitCode"] == 0
    assert migrate["FinishedAt"] <= api["StartedAt"]
    revisions = importlib.import_module("app.database")
    with fresh.db("api") as connection:
        assert connection.execute(
            "SELECT version_num FROM qa.alembic_version"
        ).fetchone() == (revisions.expected_revision(),)
        connection.execute("SELECT run_id FROM qa.runs")
    for path in ("/health/live", "/health/ready", "/healthz"):
        assert fresh.health(path) == (200, b'{"status":"ok"}')


def test_repeat_preserves_schema_and_row(migrated: Project) -> None:
    uid = uuid4()
    with migrated.db("api") as connection:
        before = connection.execute(
            "INSERT INTO qa.runs (run_id,schema_version,status) VALUES (%s,1,'CREATED') RETURNING *",
            (uid,),
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM qa.alembic_version"
        ).fetchall()
    with migrated.db() as admin:
        acl = admin.execute(
            "SELECT relname,relacl::text FROM pg_class WHERE relnamespace = 'qa'::regnamespace ORDER BY relname"
        ).fetchall()
    migrated.compose("stop", "api")
    migrated.start("--update")
    with migrated.db("api") as connection:
        assert connection.execute("SELECT * FROM qa.runs").fetchall() == [before]
        assert (
            connection.execute("SELECT version_num FROM qa.alembic_version").fetchall()
            == revision
        )
    with migrated.db() as admin:
        assert (
            admin.execute(
                "SELECT relname,relacl::text FROM pg_class WHERE relnamespace = 'qa'::regnamespace ORDER BY relname"
            ).fetchall()
            == acl
        )


def failed_transaction(
    fresh: Project, monkeypatch: pytest.MonkeyPatch, *, grant: bool
) -> None:
    migration = importlib.import_module("app.migrate")
    fresh.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
    execute = migration._execute_grants if grant else migration.command.upgrade

    def fail_after_sql(connection: Any, *args: Any) -> None:
        if grant:
            assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "3s"
            assert (
                connection.exec_driver_sql("SHOW statement_timeout").scalar_one()
                == "15s"
            )
            connection.exec_driver_sql("GRANT SELECT ON qa.runs TO qa_api")
            connection.exec_driver_sql(
                "GRANT SELECT ON qa.alembic_version TO nonexistent_test_role"
            )
        else:
            execute(connection, *args)
            connection.attributes["connection"].exec_driver_sql("SELECT 1/0")

    if grant:
        monkeypatch.setattr(migration, "_execute_grants", fail_after_sql)
    else:
        monkeypatch.setattr(migration.command, "upgrade", fail_after_sql)
    monkeypatch.setenv("QA_DB_HOST", fresh.host)
    monkeypatch.setenv("QA_DB_PASSWORD", fresh.passwords["migrate"])
    with pytest.raises(Exception) as error:
        migration.upgrade()
    assert isinstance(error.value.__cause__, Error)
    assert error.value.__cause__.sqlstate == ("42704" if grant else "22012")
    with fresh.db() as observer:
        assert observer.execute("""
            SELECT to_regnamespace('qa') IS NOT NULL,
                   to_regrole('qa_api') IS NOT NULL,
                   to_regclass('qa.runs'), to_regclass('qa.alembic_version')
        """).fetchone() == (True, True, None, None)


def test_failed_migration_rolls_back(
    fresh: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_transaction(fresh, monkeypatch, grant=False)
    # Exercise the real Compose dependency with invalid, test-only grants/roles.
    with fresh.db() as observer:
        observer.execute("ALTER SCHEMA qa OWNER TO qa_admin")
    start = fresh.start(check=False)
    assert start.returncode != 0
    assert not fresh.compose("ps", "-q", "api")
    state = cast(dict[str, Any], fresh.inspect("migrate")["State"])
    assert state["ExitCode"] != 0
    logs = fresh.compose("logs", "--no-color", "migrate")
    assert "migration failed" in logs
    assert not any(value in logs for value in fresh.passwords.values())
    assert "postgresql://" not in logs


def test_failed_grant_rolls_back(
    fresh: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_transaction(fresh, monkeypatch, grant=True)


def test_failed_update_keeps_api_stopped(migrated: Project) -> None:
    uid = uuid4()
    with migrated.db("api") as connection:
        before = connection.execute(
            "INSERT INTO qa.runs (run_id,schema_version,status) VALUES (%s,1,'CREATED') RETURNING *",
            (uid,),
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM qa.alembic_version"
        ).fetchall()
    migrate_state = migrated.inspect("migrate")["State"]
    assert migrated.start("--update", check=False).returncode != 0
    assert migrated.inspect("migrate")["State"] == migrate_state
    assert migrated.health("/health/live")[0] == 200
    migrated.compose("pause", "api")
    try:
        assert migrated.start("--update", check=False).returncode != 0
        assert migrated.inspect("migrate")["State"] == migrate_state
    finally:
        migrated.compose("unpause", "api")
    migrated.compose("stop", "api")
    with migrated.db() as observer:
        observer.execute("ALTER SCHEMA qa OWNER TO qa_admin")
    assert migrated.start("--update", check=False).returncode != 0
    assert not migrated.compose("ps", "-q", "api")
    with migrated.db("api") as connection:
        assert connection.execute("SELECT * FROM qa.runs").fetchall() == [before]
        assert (
            connection.execute("SELECT version_num FROM qa.alembic_version").fetchall()
            == revision
        )


def test_migration_process_deadline(fresh: Project) -> None:
    fresh.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
    with fresh.db() as admin:
        admin.execute("""
            CREATE FUNCTION public.delay_migration() RETURNS event_trigger
            LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(11); END; $$;
            CREATE EVENT TRIGGER delay_migration ON ddl_command_start
            WHEN TAG IN ('CREATE TABLE', 'GRANT') EXECUTE FUNCTION public.delay_migration();
        """)
    started = time.monotonic()
    assert fresh.start(check=False).returncode != 0
    assert time.monotonic() - started <= 35
    state = cast(dict[str, Any], fresh.inspect("migrate")["State"])
    assert state["ExitCode"] in (124, 137)
    assert not fresh.compose("ps", "-q", "api")
    with fresh.db() as admin:
        assert admin.execute(
            "SELECT to_regclass('qa.runs'), to_regclass('qa.alembic_version')"
        ).fetchone() == (None, None)
