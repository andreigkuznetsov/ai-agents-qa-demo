import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from psycopg import Connection
from sqlalchemy import create_engine

from app.database import Settings, migration_config

ROOT = Path(__file__).resolve().parents[2]


class Project:
    def __init__(self) -> None:
        self.name = f"qa-it-{uuid4().hex}"
        # Every project owns a new volume and generated test-only credentials.
        self.passwords = {role: uuid4().hex for role in ("admin", "migrate", "api")}
        self.env = {
            **os.environ,
            "QA_BOOTSTRAP_PASSWORD": self.passwords["admin"],
            "QA_MIGRATE_PASSWORD": self.passwords["migrate"],
            "QA_API_PASSWORD": self.passwords["api"],
            "QA_HTTP_PORT": "0",
            "QA_APP_IMAGE": os.environ.get(
                "QA_IT_IMAGE", "ai-agents-qa-demo:integration"
            ),
        }

    def compose(self, *args: str, check: bool = True) -> str:
        return self.command(
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-p",
            self.name,
            "-f",
            str(ROOT / "compose.yaml"),
            *args,
            check=check,
        ).stdout.strip()

    def command(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=110
        )
        if check and result.returncode:
            error = result.stderr + result.stdout
            for secret in self.passwords.values():
                error = error.replace(secret, "[redacted]")
            pytest.fail(f"{args[0]} exited {result.returncode}: {error}")
        return result

    def start(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self.command(
            sys.executable,
            str(ROOT / "scripts/start-local.py"),
            "--project",
            self.name,
            "--env-file",
            os.devnull,
            *args,
            check=False,
        )
        if check and result.returncode:
            error = result.stderr + result.stdout
            error += self.compose(
                "logs", "--no-color", "db", "migrate", "api", check=False
            )
            for secret in self.passwords.values():
                error = error.replace(secret, "[redacted]")
            pytest.fail(f"startup exited {result.returncode}: {error[-12000:]}")
        return result

    def inspect(self, service: str) -> dict[str, object]:
        cid = self.compose("ps", "-aq", service)
        assert cid and "\n" not in cid
        row: dict[str, object] = json.loads(
            self.command("docker", "inspect", cid).stdout
        )[0]
        return row

    @property
    def host(self) -> str:
        inspect = cast(dict[str, Any], self.inspect("db")["NetworkSettings"])
        address: str = inspect["Networks"][self.name + "_default"]["IPAddress"]
        return address

    def db(self, role: str = "admin") -> Connection[tuple[object, ...]]:
        return psycopg.connect(
            host=self.host,
            dbname="qa_demo",
            user=f"qa_{role}",
            password=self.passwords[role],
            connect_timeout=2,
        )

    def api_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QA_DB_HOST", self.host)
        monkeypatch.setenv("QA_DB_PASSWORD", self.passwords["api"])

    def health(self, path: str) -> tuple[int, bytes]:
        from urllib.error import HTTPError

        address = self.compose("port", "api", "8000")
        try:
            with urlopen(f"http://{address}{path}", timeout=5) as response:
                return response.status, response.read()
        except HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def wait_for_db_transport(self) -> None:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.compose(
                "exec", "-T", "db", "pg_isready", "-h", "127.0.0.1", check=False
            ).startswith("127.0.0.1:5432 - accepting connections"):
                return
            time.sleep(0.2)
        pytest.fail("PostgreSQL did not become ready")


@pytest.fixture
def fresh() -> Iterator[Project]:
    if os.environ.get("QA_RUN_POSTGRES_TESTS") != "1":
        pytest.skip("real PostgreSQL tests require the isolated Linux Docker job")
    project = Project()
    try:
        yield project
    finally:
        # Only this fixture's random project and its disposable volume.
        project.compose("down", "--volumes", "--remove-orphans")


@pytest.fixture
def migrated(fresh: Project) -> Project:
    fresh.start()
    return fresh


@pytest.fixture
def migrated_db(fresh: Project) -> Project:
    # Direct storage and pool tests own all API sessions in this PostgreSQL.
    fresh.compose("up", "-d", "--wait", "--wait-timeout", "40", "db")
    fresh.compose("run", "--rm", "--no-deps", "migrate")
    return fresh


def prepare_kt2(project: Project, revision: str = "0001_runs") -> None:
    """Create the historical revision in a disposable test database."""
    engine = create_engine(
        Settings(password=project.passwords["migrate"], host=project.host).url(
            "qa_migrate"
        ),
        hide_parameters=True,
    )
    try:
        with engine.begin() as conn:
            config = migration_config()
            config.attributes["connection"] = conn
            command.upgrade(config, revision)
            conn.exec_driver_sql("GRANT SELECT, INSERT ON qa.runs TO qa_api")
            conn.exec_driver_sql("GRANT SELECT ON qa.alembic_version TO qa_api")
            if revision == "0002_las_fixtures":
                conn.exec_driver_sql(
                    "GRANT SELECT ON qa.las_orders, qa.las_delivery_dates TO qa_api"
                )
    finally:
        engine.dispose()
