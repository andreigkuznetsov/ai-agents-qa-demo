import hashlib
import importlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from conftest import Project
from sqlalchemy import Connection
from support.kt3_fixture_loader import (
    FIXTURE,
    FIXTURE_VERSION,
    FixtureError,
    load_fixture,
    read_bundle,
)

from app.logistics_locks import initialization_lock, order_lock

CLIENT_A = "demo-client-a"
CLIENT_B = "demo-client-b"
SAME_ORDER = "order-001"
NOMINAL_DATE = date(2026, 11, 26)


def create_run(project: Project) -> UUID:
    run = uuid4()
    with project.db("api") as api:
        api.execute(
            "INSERT INTO qa.runs(run_id,schema_version,status) VALUES (%s,1,'CREATED')",
            (run,),
        )
    return run


def initialize(project: Project, run: UUID) -> bool:
    with project.db("migrate") as migrator:
        return load_fixture(migrator, run, FIXTURE_VERSION, FIXTURE)


def snapshot(project: Project, run: UUID) -> tuple[object, ...]:
    with project.db() as observer:
        return (
            observer.execute(
                "SELECT fixture_version,content_sha256 FROM qa.las_fixture_runs WHERE run_id=%s",
                (run,),
            ).fetchall(),
            observer.execute(
                "SELECT client_id FROM qa.las_run_clients WHERE run_id=%s ORDER BY client_id",
                (run,),
            ).fetchall(),
            observer.execute(
                "SELECT client_id,order_id,status,scheduled_delivery_date FROM qa.las_orders "
                "WHERE run_id=%s ORDER BY client_id,order_id",
                (run,),
            ).fetchall(),
            observer.execute(
                "SELECT client_id,order_id,delivery_date FROM qa.las_delivery_dates "
                "WHERE run_id=%s ORDER BY client_id,order_id,delivery_date",
                (run,),
            ).fetchall(),
        )


def test_kt3_bundle_bytes_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = FIXTURE.read_bytes()
    calls = []
    original = Path.read_bytes

    def recorded(path: Path) -> bytes:
        calls.append(path)
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", recorded)
        bundle = read_bundle(FIXTURE)
    assert calls == [FIXTURE]
    assert bundle.digest == hashlib.sha256(raw).hexdigest()
    assert NOMINAL_DATE in bundle.orders[0].options
    malformed = tmp_path / "bad.json"
    for data in (
        raw.replace(b"\n", b"\r\n"),
        b"\xef\xbb\xbf" + raw,
        b"{",
        raw.replace(b"order-001", b"order-\\ud800"),
        raw.replace(b"2026-11-26", b"2026-02-30"),
        raw.replace(b'"status": "OPEN"', b'"status": "CREATED"'),
        raw.replace(b'"status": "OPEN"', b'"status": "OPEN", "status": "OPEN"'),
    ):
        malformed.write_bytes(data)
        with pytest.raises(FixtureError):
            read_bundle(malformed)


def test_kt3_load_is_reproducible_and_scoped(migrated_db: Project) -> None:
    first, second = create_run(migrated_db), create_run(migrated_db)
    assert initialize(migrated_db, first) and initialize(migrated_db, second)
    assert snapshot(migrated_db, first) == snapshot(migrated_db, second)
    with migrated_db.db("api") as api:
        assert api.execute(
            "SELECT delivery_date FROM qa.las_delivery_dates WHERE run_id=%s AND client_id=%s AND order_id=%s ORDER BY delivery_date",
            (first, CLIENT_A, SAME_ORDER),
        ).fetchall() == [(NOMINAL_DATE,), (date(2026, 11, 27),)]
        assert api.execute(
            "SELECT delivery_date FROM qa.las_delivery_dates WHERE run_id=%s AND client_id=%s AND order_id=%s",
            (first, CLIENT_B, SAME_ORDER),
        ).fetchall() == [(NOMINAL_DATE,)]
    with migrated_db.db("migrate") as migrator:
        with pytest.raises(psycopg.errors.ForeignKeyViolation), migrator.transaction():
            migrator.execute(
                "INSERT INTO qa.las_delivery_dates VALUES (%s,%s,%s,%s)",
                (second, CLIENT_B, "order-blocked", NOMINAL_DATE),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), migrator.transaction():
            migrator.execute(
                "INSERT INTO qa.las_orders VALUES (%s,%s,%s,'ACTIVE',%s)",
                (uuid4(), CLIENT_A, SAME_ORDER, NOMINAL_DATE),
            )
    assert snapshot(migrated_db, first) == snapshot(migrated_db, second)


def test_kt3_concurrent_repeat_and_rollback(
    migrated_db: Project, tmp_path: Path
) -> None:
    run, neighbor, failed = (create_run(migrated_db) for _ in range(3))
    assert initialize(migrated_db, neighbor)
    neighbor_before = snapshot(migrated_db, neighbor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with migrated_db.db("migrate") as guard:
            guard.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)", (initialization_lock(run),)
            )
            futures = [pool.submit(initialize, migrated_db, run) for _ in range(2)]
            end = time.monotonic() + 8
            while time.monotonic() < end:
                pending = guard.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                ).fetchone()
                if pending == (2,):
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "two independent initializations did not wait on the run lock"
                )
        assert sorted(future.result(timeout=8) for future in futures) == [False, True]

    with migrated_db.db("migrate") as migrator:
        migrator.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (order_lock(run, CLIENT_A, SAME_ORDER),),
        )
        migrator.execute(
            "UPDATE qa.las_orders SET status='TRANSFER_BLOCKED' WHERE run_id=%s AND client_id=%s AND order_id=%s",
            (run, CLIENT_A, SAME_ORDER),
        )
    before = snapshot(migrated_db, run)
    assert not initialize(migrated_db, run)
    other = tmp_path / "reformatted.json"
    other.write_bytes(
        json.dumps(json.loads(FIXTURE.read_bytes()), separators=(",", ":")).encode()
    )
    for version, path in (("v2", FIXTURE), (FIXTURE_VERSION, other)):
        with migrated_db.db("migrate") as migrator:
            with pytest.raises(FixtureError, match="version or SHA-256 mismatch"):
                load_fixture(migrator, run, version, path)
    with migrated_db.db("api") as api:
        with pytest.raises(FixtureError, match="trusted migration role"):
            load_fixture(api, failed, FIXTURE_VERSION, FIXTURE)
    with migrated_db.db("migrate") as migrator:
        migrator.execute(
            "ALTER TABLE qa.las_delivery_dates ADD CONSTRAINT test_denied_dates CHECK (false) NOT VALID"
        )
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            initialize(migrated_db, failed)
        assert snapshot(migrated_db, failed) == ([], [], [], [])
    finally:
        with migrated_db.db("migrate") as migrator:
            migrator.execute(
                "ALTER TABLE qa.las_delivery_dates DROP CONSTRAINT test_denied_dates"
            )
    assert snapshot(migrated_db, run) == before
    assert snapshot(migrated_db, neighbor) == neighbor_before
    assert initialize(migrated_db, failed)

    migration = importlib.import_module("app.migrate")
    execute_grants = migration._execute_grants

    def fail_grants(conn: Connection) -> None:
        execute_grants(conn)
        conn.exec_driver_sql("GRANT SELECT ON qa.las_orders TO nonexistent_test_role")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("QA_DB_HOST", migrated_db.host)
        patch.setenv("QA_DB_PASSWORD", migrated_db.passwords["migrate"])
        with pytest.MonkeyPatch.context() as failure:
            failure.setattr(migration, "_execute_grants", fail_grants)
            with pytest.raises(Exception) as denied:
                migration.upgrade()
            assert isinstance(denied.value.__cause__, psycopg.Error)
            assert denied.value.__cause__.sqlstate == "42704"
        assert snapshot(migrated_db, run) == before
        assert snapshot(migrated_db, neighbor) == neighbor_before
        migration.upgrade()
        assert snapshot(migrated_db, run) == before
        assert snapshot(migrated_db, neighbor) == neighbor_before
