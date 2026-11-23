import hashlib
import logging
import re
import secrets
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from typing import cast
from uuid import UUID, uuid4

import pytest
from conftest import Project
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from support.kt3_fixture_loader import order_lock
from test_kt3_fixtures import CLIENT_A, CLIENT_B, SAME_ORDER, create_run, initialize

from app.logistics_auth import LogisticsContext
from app.main import app, create_app
from app.order_store import OrderStore

AUTH_ERROR = b'{"error":{"code":"UNAUTHENTICATED"}}'
MISSING = b'{"error":{"code":"RESOURCE_NOT_FOUND"}}'
ORDER = f"/orders/{SAME_ORDER}"
OPTIONS = f"{ORDER}/delivery-options"
FIRST_OPTIONS = {
    "order_id": SAME_ORDER,
    "eligible": True,
    "reason_code": None,
    "dates": ["2026-11-26", "2026-11-27"],
}


def snapshot(project: Project) -> tuple[object, ...]:
    with project.db() as observer:
        observer.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tables = (
            "runs",
            "alembic_version",
            "las_fixture_runs",
            "las_run_clients",
            "las_orders",
            "las_delivery_dates",
        )
        return tuple(
            observer.execute(
                f"SELECT to_jsonb(t) FROM qa.{name} AS t ORDER BY to_jsonb(t)"
            ).fetchall()
            for name in tables
        )


@contextmanager
def authorized_client(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
    *contexts: LogisticsContext,
    raise_server_exceptions: bool = True,
) -> Iterator[tuple[TestClient, list[str]]]:
    project.api_env(monkeypatch)
    tokens = [secrets.token_urlsafe(32) for _ in contexts]
    config = {
        hashlib.sha256(token.encode()).hexdigest(): context
        for token, context in zip(tokens, contexts, strict=True)
    }
    with TestClient(
        create_app(config), raise_server_exceptions=raise_server_exceptions
    ) as client:
        yield client, tokens


def authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_order(project: Project, run: UUID, client: str, order: str) -> None:
    with project.db("migrate") as writer:
        writer.execute(
            "INSERT INTO qa.las_orders VALUES (%s,%s,%s,'ACTIVE','2026-12-01')",
            (run, client, order),
        )
        writer.execute(
            "INSERT INTO qa.las_delivery_dates VALUES (%s,%s,%s,'2026-12-02')",
            (run, client, order),
        )


def test_kt3_order_projection_and_options(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    own, other_run = (create_run(migrated_db) for _ in range(2))
    assert initialize(migrated_db, own) and initialize(migrated_db, other_run)
    with migrated_db.db("migrate") as writer:
        scope = (other_run, CLIENT_A, SAME_ORDER)
        writer.execute(
            "UPDATE qa.las_orders SET scheduled_delivery_date='2026-12-01' "
            "WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
            scope,
        )
        writer.execute(
            "DELETE FROM qa.las_delivery_dates WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
            scope,
        )
        writer.execute(
            "INSERT INTO qa.las_delivery_dates VALUES (%s,%s,%s,'2026-12-02')",
            scope,
        )
        writer.execute(
            "INSERT INTO qa.las_delivery_dates VALUES (%s,%s,'order-blocked','2026-12-02')",
            (own, CLIENT_A),
        )
    before = snapshot(migrated_db)
    with authorized_client(
        migrated_db,
        monkeypatch,
        LogisticsContext(own, CLIENT_A),
        LogisticsContext(own, CLIENT_B),
        LogisticsContext(other_run, CLIENT_A),
    ) as (client, tokens):
        for token, scheduled, dates in zip(
            tokens,
            ["2026-11-25", "2026-11-25", "2026-12-01"],
            [["2026-11-26", "2026-11-27"], ["2026-11-26"], ["2026-12-02"]],
            strict=True,
        ):
            headers = authorization(token)
            order = client.get(ORDER, headers=headers)
            assert order.status_code == 200
            assert order.json() == {
                "order_id": SAME_ORDER,
                "status": "ACTIVE",
                "scheduled_delivery_date": scheduled,
            }
            options = client.get(OPTIONS, headers=headers)
            assert options.status_code == 200
            assert options.json() == {**FIRST_OPTIONS, "dates": dates}
        for name, code in (
            ("order-blocked", "ORDER_TRANSFER_BLOCKED"),
            ("order-no-dates", "NO_AVAILABLE_DATES"),
        ):
            response = client.get(
                f"/orders/{name}/delivery-options", headers=authorization(tokens[0])
            )
            assert response.status_code == 200
            assert response.json() == {
                "order_id": name,
                "eligible": False,
                "reason_code": code,
                "dates": [],
            }
    assert snapshot(migrated_db) == before


def test_kt3_order_authorization_and_identical_404(
    migrated_db: Project,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    own, other_run = (create_run(migrated_db) for _ in range(2))
    assert initialize(migrated_db, own) and initialize(migrated_db, other_run)
    add_order(migrated_db, own, CLIENT_B, "foreign-client-only")
    add_order(migrated_db, other_run, CLIENT_A, "foreign-run-only")
    before = snapshot(migrated_db)
    marker = f"synthetic-body-{uuid4()}"
    spoofed = {"run_id": str(other_run), "client_id": CLIENT_B}
    with (
        caplog.at_level(logging.INFO, logger="app.logistics_orders"),
        authorized_client(
            migrated_db, monkeypatch, LogisticsContext(own, CLIENT_A)
        ) as (client, tokens),
    ):
        token = tokens[0]
        for suffix in ("", "/delivery-options"):
            path = f"{ORDER}{suffix}"
            for value in (
                None,
                "Bearer unknown",
                f"Basic {token}",
                f"Bearer\t{token}",
                f"Bearer {token} extra",
                f"Bearer {token}, Bearer {token}",
            ):
                headers = {} if value is None else {"Authorization": value}
                denied = client.request("GET", path, headers=headers, params=spoofed)
                assert (denied.status_code, denied.content) == (401, AUTH_ERROR)
                assert denied.headers["www-authenticate"] == "Bearer"
            duplicates = [("Authorization", f"Bearer {token}")] * 2
            denied = client.get(path, headers=duplicates)
            assert (denied.status_code, denied.content) == (401, AUTH_ERROR)
            headers = {
                **authorization(token),
                "X-Run-Id": str(other_run),
                "X-Client-Id": CLIENT_B,
            }
            own_response = client.request(
                "GET", path, headers=headers, params=spoofed, json=spoofed
            )
            assert own_response.status_code == 200
            assert own_response.json() == (
                FIRST_OPTIONS
                if suffix
                else {
                    "order_id": SAME_ORDER,
                    "status": "ACTIVE",
                    "scheduled_delivery_date": "2026-11-25",
                }
            )
            for order in (
                "absent",
                "foreign-client-only",
                "foreign-run-only",
                "order-001' OR '1'='1",
            ):
                response = client.request(
                    "GET",
                    f"/orders/{order}{suffix}",
                    headers=headers,
                    params=spoofed,
                    json={**spoofed, "marker": marker},
                )
                assert (response.status_code, response.content) == (404, MISSING)
        for settings in ({}, authorization(token)):
            for path in ("/fixtures", "/faults", "/admin", f"/runs/{own}/fixtures"):
                assert (
                    client.post(path, headers=settings, json=spoofed).status_code == 404
                )
    with TestClient(app) as default_client:
        assert (
            default_client.get(ORDER, headers=authorization(token)).status_code == 401
        )
        assert (
            default_client.get(OPTIONS, headers=authorization(token)).status_code == 401
        )
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.logistics_orders"
    ]
    assert len(messages) == 8
    assert all(
        re.fullmatch(
            r"route=/orders/\{order_id\}(?:/delivery-options)? code=RESOURCE_NOT_FOUND correlation_id=[0-9a-f-]{36}",
            message,
        )
        for message in messages
    )
    for sensitive in (
        token,
        marker,
        str(other_run),
        "foreign-client-only",
        "foreign-run-only",
        *migrated_db.passwords.values(),
    ):
        assert sensitive not in "\n".join(messages)
    assert snapshot(migrated_db) == before


def test_kt3_options_single_snapshot_and_revision_guard(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = create_run(migrated_db)
    assert initialize(migrated_db, run)
    queried, committed = Event(), Event()
    domain_statements: list[str] = []

    def pause_after_first_read(
        _conn: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: bool,
    ) -> None:
        if (
            "qa.las_orders" not in statement
            and "qa.las_delivery_dates" not in statement
        ):
            return
        domain_statements.append(statement)
        if len(domain_statements) == 1:
            queried.set()
            assert committed.wait(5), "concurrent update did not commit"

    with authorized_client(
        migrated_db,
        monkeypatch,
        LogisticsContext(run, CLIENT_A),
        raise_server_exceptions=False,
    ) as (client, tokens):
        store: OrderStore = cast(FastAPI, client.app).state.order_store
        engine = store.database.engine.sync_engine
        event.listen(engine, "after_cursor_execute", pause_after_first_read)
        headers = authorization(tokens[0])
        try:
            with ThreadPoolExecutor(max_workers=1) as worker:
                result = worker.submit(client.get, OPTIONS, headers=headers)
                try:
                    assert queried.wait(5), "route did not issue a domain query"
                    with migrated_db.db("migrate") as writer:
                        scope = (run, CLIENT_A, SAME_ORDER)
                        writer.execute(
                            "SELECT pg_advisory_xact_lock(%s::bigint)",
                            (order_lock(*scope),),
                        )
                        writer.execute(
                            "UPDATE qa.las_orders SET status='TRANSFER_BLOCKED' "
                            "WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
                            scope,
                        )
                        writer.execute(
                            "DELETE FROM qa.las_delivery_dates WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
                            scope,
                        )
                finally:
                    committed.set()
                old = result.result(timeout=5)
            assert old.status_code == 200
            assert old.json() == FIRST_OPTIONS
            assert len(domain_statements) == 1
            new = client.get(OPTIONS, headers=headers)
            assert new.status_code == 200
            assert new.json() == {
                "order_id": SAME_ORDER,
                "eligible": False,
                "reason_code": "ORDER_TRANSFER_BLOCKED",
                "dates": [],
            }
            assert len(domain_statements) == 2
            with migrated_db.db("migrate") as writer:
                writer.execute("UPDATE qa.alembic_version SET version_num='0001_runs'")
            try:
                for path in (ORDER, OPTIONS):
                    incompatible = client.get(path, headers=headers)
                    assert incompatible.status_code >= 500
                    assert "revision" not in incompatible.text
                assert len(domain_statements) == 2
            finally:
                with migrated_db.db("migrate") as writer:
                    writer.execute(
                        "UPDATE qa.alembic_version SET version_num='0002_las_fixtures'"
                    )
        finally:
            event.remove(engine, "after_cursor_execute", pause_after_first_read)
