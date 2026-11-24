import secrets
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from conftest import Project
from test_kt3_case_result import seeded

# Run the same installed Uvicorn and application factory in the built image.
# This test-only process injects a trusted synthetic context and a single fault.
PROBE = r"""
import hashlib
import json
import socket
import sys
import time
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID

import uvicorn
from app.logistics_auth import LogisticsContext
from app.logistics_faults import ResultCheckUnavailable
from app.main import create_app

run, marker, access_log_flag = sys.argv[1:]
context = LogisticsContext(UUID(run), "demo-client-b")
checks = 0

async def fault(caller, phase):
    global checks
    if caller == context and phase == "before_result_read":
        checks += 1
        if checks == 4:
            raise ResultCheckUnavailable()

app = create_app(
    {hashlib.sha256((marker + "-bearer").encode()).hexdigest(): context},
    case_write_timeout=4, case_result_timeout=3, case_fault=fault,
)
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, access_log=access_log_flag != "--no-access-log",
    ))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started

        def call(path, *, token="-bearer", key="-key", body=None):
            request = Request(
                f"http://127.0.0.1:{port}{path}?probe={marker}-query",
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Authorization": "Bearer " + marker + token,
                    "Idempotency-Key": marker + key,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=6) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        order = "/orders/order-001"
        assert call("/cases", token="-invalid", body={"probe": marker + "-body"}) == (
            401, {"error": {"code": "UNAUTHENTICATED"}}
        )
        status, body = call(order)
        assert status == 200 and body["order_id"] == "order-001"
        assert call("/cases", body={"probe": marker + "-body"}) == (
            422, {"error": {"code": "VALIDATION_ERROR"}}
        )
        status, case = call("/cases", body={
            "order_id": "order-001", "requested_date": "2026-11-26",
            "operation_type": "DELIVERY_RESCHEDULE",
        })
        assert status == 201
        assert call(order + "/case-result") == (200, {"outcome": "FOUND", "case": case})
        assert call(order + "/case-result", key="-missing") == (
            200, {"outcome": "NOT_FOUND", "case": None}
        )
        assert call("/orders/absent/case-result") == (
            404, {"error": {"code": "RESOURCE_NOT_FOUND"}}
        )
        assert call(order + "/case-result") == (
            503, {"error": {"code": "RESULT_CHECK_UNAVAILABLE"}}
        )
        assert call("/health/live") == (200, {"status": "ok"})
        assert call("/health/ready") == (200, {"status": "ok"})
        assert call("/healthz") == (200, {"status": "ok"})
        print("runtime HTTP contracts verified")
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        assert not thread.is_alive()
"""


def test_kt3_runtime_safe_logs(migrated: Project) -> None:
    config = cast(dict[str, Any], migrated.inspect("api")["Config"])
    command = config["Cmd"]
    assert command == [
        "/app/.venv/bin/python",
        "-m",
        "uvicorn",
        "app.main:app",
        "--no-access-log",
    ]
    assert migrated.health("/health/live") == (200, b'{"status":"ok"}')
    assert migrated.health("/health/ready") == (200, b'{"status":"ok"}')
    assert migrated.health("/healthz") == (200, b'{"status":"ok"}')
    marker = "synthetic-" + secrets.token_hex(16)
    address = migrated.compose("port", "api", "8000")
    request = Request(
        f"http://{address}/cases?probe={marker}-query",
        data=f'{{"probe":"{marker}-body"}}'.encode(),
        headers={
            "Authorization": f"Bearer {marker}-invalid",
            "Idempotency-Key": f"{marker}-key",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(HTTPError) as refused:
        urlopen(request, timeout=5)
    with refused.value as error:
        assert (error.code, error.read()) == (
            401,
            b'{"error":{"code":"UNAUTHENTICATED"}}',
        )
    actual_logs = migrated.compose("logs", "--no-color", "api")
    owner = seeded(migrated)
    cid = migrated.compose("ps", "-q", "api")
    result = migrated.command(
        "docker",
        "exec",
        cid,
        command[0],
        "-c",
        PROBE,
        str(owner.run_id),
        marker,
        command[-1],
        check=False,
    )
    output = result.stdout + result.stderr + actual_logs
    if any(secret in output for secret in (marker, *migrated.passwords.values())):
        pytest.fail(
            "synthetic request marker or database credential appeared in logs",
            pytrace=False,
        )
    if any(
        text in output
        for text in ("Traceback", "postgresql+psycopg://", "[SQL:", "[parameters:")
    ):
        pytest.fail(
            "internal exception, connection string or SQL details appeared in logs",
            pytrace=False,
        )
    assert result.returncode == 0 and "runtime HTTP contracts verified" in output
    assert "code=RESULT_CHECK_UNAVAILABLE" in output and "correlation_id=" in output
    assert '"GET ' not in output and '"POST ' not in output
