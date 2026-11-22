import asyncio
from typing import Any, cast

import pytest
from conftest import Project

from app.database import Database, Settings
from app.run_store import Run, RunStore


def test_network_bindings(migrated: Project) -> None:
    bindings = cast(dict[str, Any], migrated.inspect("db")["HostConfig"])[
        "PortBindings"
    ]
    assert not bindings
    api = cast(dict[str, Any], migrated.inspect("api")["HostConfig"])["PortBindings"]
    assert [entry["HostIp"] for entry in api["8000/tcp"]] == ["127.0.0.1"]
    assert migrated.health("/health/ready")[0] == 200
    assert migrated.compose("exec", "-T", "db", "pg_isready", "-h", "db").startswith(
        "db:5432 - accepting connections"
    )


def postgres_volume(project: Project) -> str:
    mounts = cast(list[dict[str, str]], project.inspect("db")["Mounts"])
    volumes = [
        mount["Name"]
        for mount in mounts
        if mount["Type"] == "volume" and mount["Destination"] == "/var/lib/postgresql"
    ]
    assert len(volumes) == 1
    return volumes[0]


def test_runs_survive_stop_start(
    migrated: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = postgres_volume(migrated)
    uid = RunStore.new_id()

    async def access_record(*, create: bool) -> Run | None:
        db = Database(Settings.from_env())
        try:
            store = RunStore(db)
            return await store.create(uid) if create else await store.read(uid)
        finally:
            await db.close()

    migrated.api_env(monkeypatch)
    before = asyncio.run(access_record(create=True))
    assert before is not None
    migrated.compose("stop")
    migrated.start()
    assert postgres_volume(migrated) == volume
    assert migrated.health("/healthz")[0] == 200
    migrated.api_env(monkeypatch)
    assert asyncio.run(access_record(create=False)) == before
    with migrated.db("api") as observer:
        assert observer.execute(
            "SELECT count(*) FROM qa.runs WHERE run_id = %s", (uid,)
        ).fetchone() == (1,)
