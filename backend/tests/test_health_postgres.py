import time
from typing import Any, cast

import pytest
from conftest import Project

UNAVAILABLE = (503, b'{"status":"unavailable"}')


def test_db_outage(migrated: Project) -> None:
    migrated.compose("stop", "db")
    for path in ("/health/ready", "/healthz"):
        started = time.monotonic()
        assert migrated.health(path) == UNAVAILABLE
        assert time.monotonic() - started <= 3.5
    assert migrated.health("/health/live") == (200, b'{"status":"ok"}')


def test_db_recovery(migrated: Project) -> None:
    api = cast(dict[str, Any], migrated.inspect("api")["State"])["StartedAt"]
    migrated.compose("stop", "db")
    assert migrated.health("/health/ready") == UNAVAILABLE
    migrated.compose("start", "db")
    migrated.wait_for_db_transport()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and migrated.health("/health/ready")[0] != 200:
        time.sleep(0.2)
    for route in ("/health/ready", "/healthz"):
        assert migrated.health(route) == (200, b'{"status":"ok"}')
    assert cast(dict[str, Any], migrated.inspect("api")["State"])["StartedAt"] == api


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM qa.alembic_version",
        "UPDATE qa.alembic_version SET version_num = 'other'",
        "INSERT INTO qa.alembic_version VALUES ('other')",
    ],
)
def test_incompatible_revision(migrated: Project, statement: str) -> None:
    with migrated.db() as admin:
        admin.execute(statement)
    assert migrated.health("/health/ready") == UNAVAILABLE
    assert migrated.health("/healthz") == UNAVAILABLE
    assert migrated.health("/health/live") == (200, b'{"status":"ok"}')
