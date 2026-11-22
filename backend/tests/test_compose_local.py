from typing import Any, cast
from uuid import uuid4

from conftest import Project


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


def test_runs_survive_stop_start(migrated: Project) -> None:
    uid = uuid4()
    volume = postgres_volume(migrated)
    with migrated.db("api") as connection:
        before = connection.execute(
            "INSERT INTO qa.runs (run_id,schema_version,status) VALUES (%s,1,'CREATED') RETURNING *",
            (uid,),
        ).fetchone()
    migrated.compose("stop")
    migrated.start()
    assert postgres_volume(migrated) == volume
    assert migrated.health("/healthz")[0] == 200
    with migrated.db("api") as connection:
        assert connection.execute("SELECT * FROM qa.runs").fetchall() == [before]
