import hashlib
import logging
import secrets
from typing import Annotated
from uuid import uuid4

import pytest
from conftest import Project
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_kt3_fixtures import (
    CLIENT_A,
    CLIENT_B,
    SAME_ORDER,
    create_run,
    initialize,
    snapshot,
)

from app.logistics_auth import (
    LogisticsContext,
    Unauthenticated,
    logistics_auth,
    unauthenticated_response,
)
from app.main import app


def test_kt3_auth_scope_and_no_management(
    migrated_db: Project, caplog: pytest.LogCaptureFixture
) -> None:
    own, foreign = create_run(migrated_db), create_run(migrated_db)
    assert initialize(migrated_db, own) and initialize(migrated_db, foreign)
    before = snapshot(migrated_db, own), snapshot(migrated_db, foreign)
    token = secrets.token_urlsafe(32)
    unrecognized = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    trusted = {digest: LogisticsContext(own, CLIENT_A)}
    resolve = logistics_auth(trusted)
    trusted[digest] = LogisticsContext(foreign, CLIENT_B)
    probe = FastAPI()
    probe.add_exception_handler(Unauthenticated, unauthenticated_response)

    @probe.post("/scope-probe")
    def scope_probe(
        context: Annotated[LogisticsContext, Depends(resolve)],
    ) -> dict[str, object]:
        with migrated_db.db("api") as db:
            rows = db.execute(
                "SELECT delivery_date FROM qa.las_delivery_dates "
                "WHERE run_id=%s AND client_id=%s AND order_id=%s ORDER BY delivery_date",
                (context.run_id, context.client_id, SAME_ORDER),
            ).fetchall()
        return {
            "run_id": str(context.run_id),
            "client_id": context.client_id,
            "dates": [str(row[0]) for row in rows],
        }

    auth = {"Authorization": f"Bearer {token}"}
    spoofed = {"run_id": str(foreign), "client_id": CLIENT_B}
    body_marker = f"synthetic-request-body-{uuid4()}"
    body = {**spoofed, "marker": body_marker}
    with caplog.at_level(logging.DEBUG), TestClient(probe) as client:
        for headers in (
            {},
            {"Authorization": f"Bearer {foreign}"},
            {"Authorization": f"Bearer {unrecognized}"},
        ):
            response = client.post("/scope-probe", headers=headers, json=body)
            assert response.status_code == 401
            assert response.json() == {"error": {"code": "UNAUTHENTICATED"}}
        duplicate = client.post(
            "/scope-probe", headers=[("Authorization", auth["Authorization"])] * 2
        )
        assert duplicate.status_code == 401
        response = client.post(
            "/scope-probe",
            headers={**auth, "X-Run-Id": str(foreign), "X-Client-Id": CLIENT_B},
            params=spoofed,
            json=body,
        )
        assert response.status_code == 200
        assert response.json() == {
            "run_id": str(own),
            "client_id": CLIENT_A,
            "dates": ["2026-11-26", "2026-11-27"],
        }
    with TestClient(app) as client:
        for path in ("/fixtures", "/faults", "/admin", f"/runs/{uuid4()}/fixtures"):
            assert client.post(path, headers=auth, json=spoofed).status_code == 404
    assert (snapshot(migrated_db, own), snapshot(migrated_db, foreign)) == before
    assert caplog.records
    for sensitive in (
        token,
        digest,
        unrecognized,
        body_marker,
        *migrated_db.passwords.values(),
        "postgresql://",
        "postgresql+psycopg://",
    ):
        assert sensitive not in caplog.text
