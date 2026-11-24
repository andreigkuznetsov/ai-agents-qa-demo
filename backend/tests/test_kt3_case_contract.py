from uuid import UUID, uuid4

import pytest
from conftest import Project
from test_kt3_fixtures import CLIENT_A, CLIENT_B, SAME_ORDER, create_run, initialize
from test_kt3_order_reads import MISSING, authorization, authorized_client, snapshot

from app.case_store import OPERATION
from app.logistics_auth import LogisticsContext
from app.logistics_locks import order_lock

PAYLOAD = {
    "order_id": SAME_ORDER,
    "requested_date": "2026-11-26",
    "operation_type": OPERATION,
}
CASE_FIELDS = {"case_id", "order_id", "requested_date", "operation_type", "status"}


def test_case_http_contract(
    migrated_db: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, neighbor = create_run(migrated_db), create_run(migrated_db)
    assert initialize(migrated_db, run) and initialize(migrated_db, neighbor)
    with authorized_client(
        migrated_db,
        monkeypatch,
        LogisticsContext(run, CLIENT_A),
        LogisticsContext(run, CLIENT_B),
        LogisticsContext(neighbor, CLIENT_B),
        case_write_timeout=4,
    ) as (client, tokens):
        a, b, other = [authorization(token) for token in tokens]
        before = snapshot(migrated_db)
        assert client.post("/cases", content=b"{").status_code == 401
        assert client.post("/cases", headers=b, content=b"{").json() == {
            "error": {"code": "VALIDATION_ERROR"}
        }
        for body in (
            {},
            {**PAYLOAD, "order_id": 1},
            {**PAYLOAD, "requested_date": "20261126"},
            {**PAYLOAD, "operation_type": "OTHER"},
            {**PAYLOAD, "run_id": str(neighbor)},
        ):
            refusal = client.post(
                "/cases", headers={**b, "Idempotency-Key": "key"}, json=body
            )
            assert (refusal.status_code, refusal.json()) == (
                422,
                {"error": {"code": "VALIDATION_ERROR"}},
            )
        assert client.post("/cases", headers=b, json=PAYLOAD).status_code == 422
        for route in (f"/orders/{SAME_ORDER}/cases", f"/cases/{uuid4()}"):
            assert client.get(route).status_code == 401
        assert client.get(f"/orders/{SAME_ORDER}/cases", headers=b).json() == {
            "cases": []
        }
        assert client.post("/fixtures", headers=b).status_code == 404
        for order in ("absent", "order-blocked"):
            refusal = client.post(
                "/cases",
                headers={**b, "Idempotency-Key": "key"},
                json={**PAYLOAD, "order_id": order},
            )
            assert (refusal.status_code, refusal.content) == (404, MISSING)
        assert snapshot(migrated_db) == before

        seed = client.get(f"/orders/{SAME_ORDER}/cases", headers=a).json()["cases"][0]
        duplicate = client.post(
            "/cases", headers={**a, "Idempotency-Key": "key"}, json=PAYLOAD
        )
        assert (duplicate.status_code, duplicate.json()) == (200, seed)
        result = client.post(
            "/cases", headers={**b, "Idempotency-Key": "key"}, json=PAYLOAD
        )
        assert result.status_code == 201
        case = result.json()
        assert set(case) == CASE_FIELDS and UUID(case["case_id"])
        assert {field: case[field] for field in PAYLOAD} == PAYLOAD
        assert case["status"] == "OPEN"
        third = client.post(
            "/cases", headers={**other, "Idempotency-Key": "key"}, json=PAYLOAD
        )
        assert third.status_code == 201
        assert len({seed["case_id"], case["case_id"], third.json()["case_id"]}) == 3
        before_reads = snapshot(migrated_db)
        assert client.get(f"/orders/{SAME_ORDER}/cases", headers=b).json() == {
            "cases": [case]
        }
        assert client.get(f"/cases/{case['case_id']}", headers=b).json() == case
        for headers in (a, other):
            assert (
                client.get(f"/cases/{case['case_id']}", headers=headers).content
                == MISSING
            )
        assert client.get(f"/cases/{uuid4()}", headers=b).content == MISSING
        assert client.get("/orders/order-blocked/cases", headers=b).content == MISSING
        assert snapshot(migrated_db) == before_reads
        with migrated_db.db() as observer:
            assert observer.execute(
                "SELECT status,scheduled_delivery_date::text FROM qa.las_orders "
                "WHERE run_id=%s AND client_id=%s AND order_id=%s",
                (run, CLIENT_B, SAME_ORDER),
            ).fetchone() == ("ACTIVE", "2026-11-25")
            assert observer.execute(
                "SELECT case_id FROM qa.las_idempotency_keys WHERE run_id=%s AND client_id=%s AND request_key='key'",
                (run, CLIENT_B),
            ).fetchone() == (UUID(case["case_id"]),)

        for action in ("delete date", "block"):
            with migrated_db.db("migrate") as writer:
                scope = (run, CLIENT_B, SAME_ORDER)
                writer.execute(
                    "SELECT pg_advisory_xact_lock(%s::bigint)", (order_lock(*scope),)
                )
                if action == "delete date":
                    writer.execute(
                        "DELETE FROM qa.las_delivery_dates WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
                        scope,
                    )
                    expected = (422, "DELIVERY_DATE_NOT_ALLOWED")
                else:
                    writer.execute(
                        "UPDATE qa.las_orders SET status='TRANSFER_BLOCKED' WHERE (run_id,client_id,order_id)=(%s,%s,%s)",
                        scope,
                    )
                    expected = (409, "ORDER_TRANSFER_BLOCKED")
            unchanged = snapshot(migrated_db)
            replay = client.post(
                "/cases",
                headers={**b, "Idempotency-Key": "key"},
                content=b'{ "requested_date":"2026-11-26","operation_type":"DELIVERY_RESCHEDULE","order_id":"order-00\\u0031" }',
            )
            assert (replay.status_code, replay.json()) == (200, case)
            conflict = client.post(
                "/cases",
                headers={**b, "Idempotency-Key": "key"},
                json={**PAYLOAD, "requested_date": "2026-11-27"},
            )
            assert (conflict.status_code, conflict.json()) == (
                409,
                {"error": {"code": "IDEMPOTENCY_KEY_CONFLICT"}},
            )
            new = client.post(
                "/cases", headers={**b, "Idempotency-Key": "new"}, json=PAYLOAD
            )
            assert (new.status_code, new.json()["error"]["code"]) == expected
            forbidden = client.post(
                "/cases",
                headers={**b, "Idempotency-Key": "key"},
                json={**PAYLOAD, "order_id": "order-blocked"},
            )
            assert (forbidden.status_code, forbidden.content) == (404, MISSING)
            assert snapshot(migrated_db) == unchanged
