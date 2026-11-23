"""Exercise the existing KT2 stack in disposable, explicitly scoped Compose projects."""

import argparse
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
VERSION_KEYS = ("python", "docker", "engine", "compose", "desktop")
REQUIRED = {
    "linux-ci": (
        "preflight", "build", "startup", "stop", "persistence",
        "database-loss", "database-recovery", "migration-failure", "cleanup",
    ),
    "windows": ("preflight", "build", "startup", "stop", "persistence", "cleanup"),
    "linux": ("preflight", "build", "startup", "stop", "persistence", "cleanup"),
}
COMMANDS = {
    "preflight": "git status/rev-parse; docker context inspect/info/version; docker compose version",
    "build": "docker compose build api",
    "startup": "scripts/start-local.py; docker inspect; HTTP live/ready/healthz; psql",
    "stop": "docker compose stop api db; docker inspect",
    "persistence": "docker compose exec api (RunStore); scripts/start-local.py; psql; HTTP",
    "database-loss": "docker compose stop db; HTTP live/ready/healthz",
    "database-recovery": "docker compose up db; HTTP ready/healthz; docker inspect",
    "migration-failure": "docker compose up db; psql; scripts/start-local.py; docker inspect/logs",
    "cleanup": "docker compose stop; docker inspect",
}
CODES = {
    "timeout", "command-failed", "ownership", "context", "environment",
    "checkout", "ci-environment", "collision", "network", "http",
    "revision", "migration-order", "migration-failure", "persistence",
    "report", "interrupted", "unexpected", "engine-unavailable",
}
PROJECT = r"kt2caf-[0-9a-f]{24}-(?:positive|negative)"
SHA = r"[0-9a-f]{40}"
REV = r"[a-z0-9_]{1,32}"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][A-Za-z0-9.-]+)?"
RUN_URL = r"https://github\.com/andreigkuznetsov/ai-agents-qa-demo/actions/runs/[0-9]+"


class Failure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise Failure(code)


def local_endpoint(mode, endpoint):
    if mode == "windows":
        return endpoint in (
            "npipe:////./pipe/dockerDesktopLinuxEngine", "npipe:////./pipe/docker_engine",
        )
    return endpoint == "unix:///var/run/docker.sock" or bool(
        re.fullmatch(r"unix:///run/user/[0-9]+/docker\.sock", endpoint)
    )


def check_environment(env, context):
    for name in env:
        key = name.upper()
        require(
            key not in {"DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_TLS", "DOCKER_TLS_VERIFY",
                        "DOCKER_CERT_PATH", "DOCKER_API_VERSION"}
            and not key.startswith(("COMPOSE_", "QA_")),
            "environment",
        )
        if key == "DOCKER_CONTEXT":
            require(env[name] == context, "environment")


def owned(labels, project, kind, name):
    require(
        labels.get("com.docker.compose.project") == project
        and labels.get(f"com.docker.compose.{kind}") == name,
        "ownership",
    )


def same(actual, previous):
    require(all(actual.get(key) == val for key, val in previous.items()), "ownership")


def remaining(deadline, cap):
    budget = min(cap, deadline - monotonic())
    require(budget > 0, "timeout")
    return budget


def invoke(args, timeout, env):
    try:
        return subprocess.run(
            args, cwd=ROOT, env=env, timeout=timeout, capture_output=True,
            text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise Failure("timeout") from None
    except OSError:
        raise Failure("command-failed") from None


def stdout(result):
    require(result.returncode == 0, "command-failed")
    return result.stdout.strip()


def health_status(code, body, elapsed):
    allowed = {200: {"status": "ok"}, 503: {"status": "unavailable"}}
    require(code in allowed and body == allowed[code] and elapsed <= 3.5, "http")
    return code


def migration_order(migrate, api):
    require(
        migrate["ExitCode"] == 0
        and compose_time(migrate["FinishedAt"]) <= compose_time(api["StartedAt"]),
        "migration-order",
    )


def compose_time(value):
    # Docker uses up to nine fractional digits.
    whole, _, fraction = value.removesuffix("Z").partition(".")
    return whole, fraction.ljust(9, "0")


def failed_migration(start_exit, migrate_exit, stage, safe_log, api_started, tables):
    require(
        (start_exit, migrate_exit, stage, safe_log, api_started, tables)
        == (1, 1, True, True, False, False),
        "migration-failure",
    )


def persisted(before, after, count):
    require(isinstance(before, list) and len(before) == 4 and before == after and count == 1,
            "persistence")
    try:
        run_id, version, created_at, status = before
        valid = (UUID(run_id).version == 4 and type(version) is int and version == 1
                 and datetime.fromisoformat(created_at).utcoffset().total_seconds() == 0
                 and status == "CREATED")
    except (ValueError, TypeError, AttributeError):
        valid = False
    require(valid, "persistence")


def cleanup(projects, first_error):
    for project in reversed(projects):
        try:
            project.stop()
        except Failure as error:
            first_error = first_error or error
    return first_error


def make_report(sha, mode):
    return {
        "sha": sha, "platform": mode, "status": "NOT VERIFIED" if mode == "windows" else "FAIL",
        "versions": {}, "run": "", "checks": {}, "projects": {},
    }


# Evidence values come from selected fields and fixed enums. No opaque command output enters a report.
FACTS = {
    "startup": {"start_ms": r"[0-9]{1,5}", "revision": REV,
                "migration_exit": r"0", "migrate_before_api": r"yes",
                "binding": r"127\.0\.0\.1:[0-9]{1,5}", "postgres_port": r"unpublished",
                "live": r"200", "ready": r"200", "healthz": r"200"},
    "stop": {"volume": r"preserved"},
    "persistence": {"run_id": r"[a-f0-9-]{36}", "schema_version": r"1",
                    "created_at": r"[0-9T:.\-]+\+00:00", "status": r"CREATED",
                    "count": r"1", "start_ms": r"[0-9]{1,5}", "migration_exit": r"0",
                    "revision": REV, "ready": r"200"},
    "database-loss": {"live": r"200", "ready": r"503", "healthz": r"503",
                      "ready_ms": r"[0-9]{1,4}", "healthz_ms": r"[0-9]{1,4}"},
    "database-recovery": {"ready": r"200", "healthz": r"200", "api": r"same",
                          "ready_ms": r"[0-9]{1,4}", "healthz_ms": r"[0-9]{1,4}"},
    "migration-failure": {"migration_exit": r"1", "diagnostic": r"migration-configuration",
                          "api": r"never-started", "tables": r"absent"},
    "cleanup": {"volumes": r"preserved"},
}


def validate_report(report, expected_sha):
    require(isinstance(report, dict) and set(report) == set(make_report("", "linux")), "report")
    mode = report["platform"]
    require(isinstance(mode, str) and mode in REQUIRED and
            isinstance(report["sha"], str) and re.fullmatch(SHA, report["sha"])
            and report["sha"] == expected_sha, "report")
    status = report["status"]
    require(status in ("PASS", "FAIL", "NOT VERIFIED") and
            (status != "NOT VERIFIED" or mode == "windows"), "report")
    versions, checks, projects = (report[key] for key in ("versions", "checks", "projects"))
    require(all(isinstance(value, dict) for value in (versions, checks, projects)), "report")
    for name, value in versions.items():
        require(name in VERSION_KEYS and isinstance(value, str) and re.fullmatch(VERSION, value),
                "report")
    require(isinstance(report["run"], str) and
            (report["run"] == "" or (mode == "linux-ci" and re.fullmatch(RUN_URL, report["run"]))),
            "report")
    for purpose, project in projects.items():
        require(purpose in ("positive", "negative") and isinstance(project, str)
                and re.fullmatch(PROJECT, project) and project.endswith("-" + purpose), "report")
    for name, check in checks.items():
        require(name in REQUIRED[mode] and isinstance(check, dict)
                and set(check) <= {"result", "ms", "code", "facts"}
                and check.get("result") in ("PASS", "FAIL") and type(check.get("ms")) is int
                and 0 <= check["ms"] <= 900000, "report")
        if check["result"] == "FAIL":
            require(check.get("code") in CODES and "facts" not in check, "report")
        else:
            expected = FACTS.get(name, {})
            facts = check.get("facts", {})
            require("code" not in check and isinstance(facts, dict) and set(facts) == set(expected),
                    "report")
            for key, pattern in expected.items():
                require(isinstance(facts[key], str) and re.fullmatch(pattern, facts[key]), "report")
    if status == "PASS":
        expected_projects = {"positive", "negative"} if mode == "linux-ci" else {"positive"}
        required_versions = set(VERSION_KEYS if mode == "windows" else VERSION_KEYS[:-1])
        require(set(checks) == set(REQUIRED[mode]) and set(projects) == expected_projects
                and required_versions <= set(versions)
                and all(item["result"] == "PASS" for item in checks.values())
                and (mode != "linux-ci" or report["run"]), "report")
    elif status == "NOT VERIFIED":
        require(set(checks) <= {"preflight"}, "report")
    elif checks:
        require(any(item["result"] == "FAIL" for item in checks.values()), "report")


def write_report(dir_path, report, sensitive):
    validate_report(report, report["sha"])
    encoded = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    require(all(value not in encoded for value in sensitive), "report")
    title = f'# Compose acceptance: {report["status"]}'
    lines = [title, "", f'- Commit: {report["sha"]}', f'- Platform: {report["platform"]}']
    lines += [f"- {tool}: {version}" for tool, version in report["versions"].items()]
    lines += [f'- Run: {report["run"]}'] if report["run"] else []
    lines += ["", "| Check | Commands | Result | ms |", "| --- | --- | --- | ---: |"]
    for name, check in report["checks"].items():
        result = check.get("code", check["result"])
        lines.append(f'| {name} | {COMMANDS[name]} | {result} | {check["ms"]} |')
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, content in (("report.json", encoded), ("summary.md", "\n".join(lines) + "\n")):
        with (dir_path / name).open("x", encoding="utf-8", newline="\n") as destination:
            destination.write(content)



def version(value):
    match = re.fullmatch(r"v?(" + VERSION + r")", value.strip())
    require(match, "context")
    return match[1]


def checkout(sha, env):
    for command, expected in (
        (["rev-parse", "HEAD"], sha),
        (["status", "--porcelain", "--untracked-files=all"], ""),
    ):
        require(stdout(invoke(["git", *command], 10, env)) == expected, "checkout")


class Engine:
    def __init__(self, mode, context, env, versions):
        self.env = {k: v for k, v in env.items() if k.upper() != "DOCKER_CONTEXT"}
        endpoint = stdout(invoke(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", context],
            8, self.env,
        ))
        require(local_endpoint(mode, endpoint), "context")
        self.env["DOCKER_HOST"] = endpoint
        try:
            info = self.info()
        except Failure:
            raise Failure("engine-unavailable") from None
        require(info["OSType"] == "linux" and info["ID"], "context")
        self.identity = {key: info[key] for key in ("ID", "OSType", "OperatingSystem")}
        if mode == "windows":
            require(info["OperatingSystem"] == "Docker Desktop", "context")
            # Older Docker Desktop releases do not provide the desktop CLI plugin.
            desktop = stdout(invoke([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Item -LiteralPath (Join-Path $env:ProgramFiles "
                "'Docker/Docker/Docker Desktop.exe') -ErrorAction Stop).VersionInfo.ProductVersion",
            ], 8, self.env))
            versions["desktop"] = version(desktop)
        versions.update({
            "python": version(platform.python_version()),
            "docker": version(self.run("version", "--format", "{{.Client.Version}}")),
            "engine": version(info["ServerVersion"]),
            "compose": version(self.run("compose", "version", "--short")),
        })

    def info(self):
        return json.loads(self.run("info", "--format",
            '{"ID":{{json .ID}},"OSType":{{json .OSType}},'
            '"OperatingSystem":{{json .OperatingSystem}},"ServerVersion":{{json .ServerVersion}}}'))

    def ensure(self):
        same(self.info(), self.identity)

    def run(self, *args, timeout=8):
        return stdout(invoke(["docker", *args], timeout, self.env))


class Project:
    def __init__(self, engine, directory, nonce, purpose, image, sensitive):
        self.engine = engine
        self.name = f"kt2caf-{nonce}-{purpose}"
        self.volume = self.name + "_postgres"
        self.network = self.name + "_default"
        self.containers = {}
        self.storage = {}
        self.started = False
        self.env_file = directory / (self.name + ".env")
        self.assert_fresh()
        credentials = [secrets.token_hex(24) for _ in range(3)]
        sensitive.extend(credentials)
        env_file = dict(zip(
            ("QA_BOOTSTRAP_PASSWORD", "QA_MIGRATE_PASSWORD", "QA_API_PASSWORD"), credentials,
        ))
        env_file.update(QA_APP_IMAGE=image, QA_HTTP_PORT="0")
        with self.env_file.open("x", encoding="ascii", newline="\n") as destination:
            destination.writelines(f"{key}={value}\n" for key, value in env_file.items())
        self.args = [
            "--file", str(ROOT / "compose.yaml"), "--project-name", self.name,
            "--env-file", str(self.env_file),
        ]

    def assert_fresh(self):
        label = f"label=com.docker.compose.project={self.name}"
        engine = self.engine
        engine.ensure()
        require(
            not engine.run("ps", "-aq", "--filter", label)
            and not engine.run("volume", "ls", "-q", "--filter", label)
            and not engine.run("network", "ls", "-q", "--filter", label)
            and self.volume not in engine.run("volume", "ls", "-q").splitlines()
            and self.network not in engine.run("network", "ls", "--format", "{{.Name}}").splitlines()
            and not any(name.startswith(self.name + "-") for name in
                        engine.run("ps", "-a", "--format", "{{.Names}}").splitlines()),
            "collision",
        )

    def compose(self, *args, timeout=8):
        return self.engine.run("compose", *self.args, *args, timeout=timeout)

    def inspect(self, kind, identifier, fields):
        template = "{" + ",".join(f'"{key}":{{{{json .{field}}}}}' for key, field in fields.items()) + "}"
        return json.loads(self.engine.run(kind, "inspect", "--format", template, identifier))

    def verify(self, changes=(), *, created=False):
        """Permit new/recreated containers only after this driver requests creation."""
        self.engine.ensure()
        volume = self.inspect("volume", self.volume, {
            "Name": "Name", "CreatedAt": "CreatedAt", "Labels": "Labels", "Driver": "Driver",
        })
        network = self.inspect("network", self.network, {
            "Id": "Id", "Name": "Name", "Labels": "Labels",
        })
        owned(volume["Labels"] or {}, self.name, "volume", "postgres")
        owned(network["Labels"] or {}, self.name, "network", "default")
        require(volume["Name"] == self.volume and volume["Driver"] == "local"
                and volume["CreatedAt"] and network["Name"] == self.network, "ownership")
        storage = {"volume": volume, "network": network}
        if self.storage:
            same(storage, self.storage)
        else:
            require(created, "ownership")

        observed = {}
        all_ids = set(self.compose("ps", "--all", "--quiet").splitlines())
        for identifier in all_ids:
            obj = self.inspect("container", identifier, {
                "Id": "Id", "Name": "Name", "Labels": "Config.Labels", "Mounts": "Mounts",
                "Image": "Image", "State": "State", "Ports": "NetworkSettings.Ports",
                "Bindings": "HostConfig.PortBindings",
            })
            labels = obj["Labels"] or {}
            service = labels.get("com.docker.compose.service")
            require(service in {"db", "api", "migrate"} and service not in observed, "ownership")
            owned(labels, self.name, "service", service)
            require(obj["Id"] == identifier and obj["Name"] == f"/{self.name}-{service}-1",
                    "ownership")
            if service == "db":
                volumes = [mount.get("Name") for mount in obj["Mounts"] if mount["Type"] == "volume"]
                require(volumes == [self.volume], "ownership")
            if service in self.containers and service not in changes:
                require(identifier == self.containers[service], "ownership")
            if service not in self.containers:
                require(created and service in changes, "ownership")
            observed[service] = obj
        require(set(self.containers) <= set(observed) and "db" in observed, "ownership")
        actual = set(self.engine.run("ps", "-aq", "--no-trunc", "--filter",
                    f"label=com.docker.compose.project={self.name}").splitlines())
        require(actual == all_ids, "ownership")
        self.storage = storage
        self.containers = {service: obj["Id"] for service, obj in observed.items()}
        return observed

    def build(self):
        self.engine.ensure()
        self.compose("build", "api", timeout=300)

    def start(self, *, failure=False, update=False):
        if self.started:
            self.verify()
        else:
            self.assert_fresh()
            self.started = True
        start = monotonic()
        args = [
            sys.executable, str(ROOT / "scripts/start-local.py"), "--project", self.name,
            "--env-file", str(self.env_file),
        ]
        result = invoke(args + (["--update"] if update else []), 90, self.engine.env)
        elapsed_ms = round((monotonic() - start) * 1000)
        changes = ("migrate", "api") if "db" in self.containers else ("db", "migrate", "api")
        observed = self.verify(changes, created=True)
        if failure:
            return result, observed
        require(result.returncode == 0 and elapsed_ms <= 90000, "command-failed")
        require({"db", "migrate", "api"} == set(observed)
                and observed["api"]["State"]["Running"]
                and observed["api"]["Image"] == observed["migrate"]["Image"], "migration-order")
        migration_order(observed["migrate"]["State"], observed["api"]["State"])
        return elapsed_ms, observed

    def stop(self, *services):
        if not self.started:
            return
        observed = self.verify()
        targets = services or tuple(observed)
        require(all(service in observed for service in targets), "ownership")
        self.compose("stop", "--timeout", "8", *targets, timeout=18)
        remaining_resources = self.verify()
        require(all(not remaining_resources[service]["State"]["Running"]
                    for service in targets), "ownership")

    def psql(self, sql):
        return self.compose(
            "exec", "-T", "db", "sh", "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -w -h 127.0.0.1 '
            '-U qa_admin -d qa_demo -v ON_ERROR_STOP=1 -Atc "$1"', "psql", sql,
        )

    def api_python(self, source, *args):
        return self.compose("exec", "-T", "api", "/app/.venv/bin/python", "-c", source, *args)

    def revision(self):
        bundled = self.api_python("from app.database import expected_revision; print(expected_revision())")
        actual = self.psql("SELECT version_num FROM qa.alembic_version")
        require(re.fullmatch(REV, bundled) and actual == bundled, "revision")
        return actual


def binding(observed):
    db, api = observed["db"], observed["api"]
    require(not db["Bindings"] and not any((db["Ports"] or {}).values()), "network")
    ports = api["Ports"]
    require(set(ports) == {"8000/tcp"} and len(ports["8000/tcp"]) == 1, "network")
    bound = ports["8000/tcp"][0]
    require(bound["HostIp"] == "127.0.0.1" and 0 < int(bound["HostPort"]) <= 65535, "network")
    return int(bound["HostPort"])


def http(port, path, expected, *, deadline=None):
    # The numeric loopback address bypasses proxies and redirects.
    while True:
        budget = 3.5 if deadline is None else remaining(deadline, 3.5)
        started = monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=budget)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            payload = json.loads(response.read(512))
            elapsed = monotonic() - started
            observed = health_status(response.status, payload, elapsed)
        except (OSError, http.client.HTTPException, ValueError):
            raise Failure("http") from None
        finally:
            conn.close()
        if observed == expected:
            return round(elapsed * 1000)
        require(deadline is not None and observed == 503 and expected == 200, "http")
        sleep(remaining(deadline, 0.35))


RUN_STORE_CODE = """\
import asyncio
import json
import sys
from uuid import UUID
from app.database import Database, Settings
from app.run_store import RunStore

async def main():
    database = Database(Settings.from_env())
    try:
        store = RunStore(database)
        row = (await store.read(UUID(sys.argv[1])) if len(sys.argv) > 1
               else await store.create(store.new_id()))
        print(json.dumps([str(row.run_id), row.schema_version, row.created_at.isoformat(), row.status]
                         if row else None))
    finally:
        await database.close()

asyncio.run(main())
"""


def positive(project, check, mode):
    check("build", project.build)
    port, revision, row, api_state = None, None, None, None

    def startup():
        nonlocal port, revision, row
        elapsed_ms, observed = project.start()
        port = binding(observed)
        revision = project.revision()
        for path in ("/health/live", "/health/ready", "/healthz"):
            http(port, path, 200)
        row = json.loads(project.api_python(RUN_STORE_CODE))
        persisted(row, row, 1)
        return {
            "start_ms": elapsed_ms, "revision": revision, "migration_exit": 0,
            "migrate_before_api": "yes", "binding": f"127.0.0.1:{port}",
            "postgres_port": "unpublished", "live": 200, "ready": 200, "healthz": 200,
        }

    check("startup", startup)

    def stop():
        project.stop("api", "db")
        return {"volume": "preserved"}

    check("stop", stop)

    def persistence():
        nonlocal port, api_state
        elapsed_ms, observed = project.start(update=True)
        port = binding(observed)
        require(project.revision() == revision, "revision")
        http(port, "/health/ready", 200)
        after = json.loads(project.api_python(RUN_STORE_CODE, row[0]))
        count = int(project.psql("SELECT count(*) FROM qa.runs"))
        persisted(row, after, count)
        api_state = (observed["api"]["Id"], observed["api"]["State"]["StartedAt"])
        return dict(zip(("run_id", "schema_version", "created_at", "status"), row)) | {
            "count": count, "start_ms": elapsed_ms, "migration_exit": 0, "revision": revision,
            "ready": 200,
        }

    check("persistence", persistence)
    if mode != "linux-ci":
        return

    def loss():
        project.stop("db")
        http(port, "/health/live", 200)
        return {"live": 200, "ready": 503, "healthz": 503,
                "ready_ms": http(port, "/health/ready", 503),
                "healthz_ms": http(port, "/healthz", 503)}

    def recovery():
        project.verify()
        project.compose("up", "-d", "--no-build", "--no-recreate", "--wait", "--wait-timeout", "39",
                        "db", timeout=40)
        deadline = monotonic() + 15
        ready_ms = http(port, "/health/ready", 200, deadline=deadline)
        healthz_ms = http(port, "/healthz", 200, deadline=deadline)
        api = project.verify()["api"]
        require((api["Id"], api["State"]["StartedAt"]) == api_state, "ownership")
        return {"ready": 200, "healthz": 200, "api": "same",
                "ready_ms": ready_ms, "healthz_ms": healthz_ms}

    check("database-loss", loss)
    check("database-recovery", recovery)


def negative(project):
    project.assert_fresh()
    project.started = True
    project.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "39", "db", timeout=40)
    project.verify(("db",), created=True)
    tables = "SELECT to_regclass('qa.runs') IS NULL AND to_regclass('qa.alembic_version') IS NULL"
    require(project.psql(tables) == "t", "migration-failure")
    project.psql("ALTER SCHEMA qa OWNER TO qa_admin")
    result, observed = project.start(failure=True)
    migrate = observed["migrate"]
    api = observed.get("api")
    log = project.engine.run("container", "logs", migrate["Id"])
    # Docker logs may send stderr separately. The Compose start subprocess captures both.
    expected = "migration failed: PostgreSQL configuration, revision or transaction (RuntimeError)"
    failed_migration(
        result.returncode, migrate["State"]["ExitCode"],
        "Startup failed during migration. Check Compose service state." in result.stdout.splitlines(),
        expected in log or expected in result.stdout or expected in result.stderr,
        bool(api and not api["State"]["StartedAt"].startswith("0001-01-01")),
        project.psql(tables) != "t",
    )
    return {"migration_exit": 1, "diagnostic": "migration-configuration",
            "api": "never-started", "tables": "absent"}


def perform(mode, sha, context, report_dir):
    report = make_report(sha, mode)
    projects, sensitive = [], []
    env = dict(os.environ)
    phase = "preflight"

    def check(name, action):
        nonlocal phase
        phase = name
        started = monotonic()
        try:
            facts = action()
        except Failure as error:
            report["checks"][name] = {"result": "FAIL", "ms": round((monotonic() - started) * 1000),
                                      "code": error.code}
            raise
        entry = {"result": "PASS", "ms": round((monotonic() - started) * 1000)}
        if facts:
            entry["facts"] = {key: str(value) for key, value in facts.items()}
        report["checks"][name] = entry

    def preflight():
        require((mode == "windows" and sys.platform == "win32")
                or (mode != "windows" and sys.platform.startswith("linux")), "context")
        if mode == "linux-ci":
            run = env.get("GITHUB_RUN_ID", "")
            require(env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
                    and env.get("GITHUB_REPOSITORY") == "andreigkuznetsov/ai-agents-qa-demo"
                    and run.isascii() and run.isdigit(), "ci-environment")
            report["run"] = "https://github.com/andreigkuznetsov/ai-agents-qa-demo/actions/runs/" + run
        check_environment(env, context)
        checkout(sha, env)
        return Engine(mode, context, env, report["versions"])

    engine = None

    def select_engine():
        nonlocal engine
        engine = preflight()

    error = None
    with tempfile.TemporaryDirectory(prefix="kt2-compose-") as tmp:
        try:
            check("preflight", select_engine)
            nonce = secrets.token_hex(12)
            image = f"ai-agents-qa-demo:kt2caf-{nonce}"
            require(image not in engine.run("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines(),
                    "collision")

            def project_for(purpose):
                project = Project(engine, Path(tmp), nonce, purpose, image, sensitive)
                projects.append(project)
                report["projects"][purpose] = project.name
                return project

            positive(project_for("positive"), check, mode)
            if mode == "linux-ci":
                check("migration-failure", lambda: negative(project_for("negative")))
        except Failure as problem:
            error = problem
        except KeyboardInterrupt:
            error = Failure("interrupted")
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            error = Failure("unexpected")

        if error:
            # A failure between measured steps still invalidates the run.
            report["checks"][phase] = report["checks"].get(phase, {})
            if report["checks"][phase].get("result") != "FAIL":
                report["checks"][phase] = {"result": "FAIL", "ms": 0, "code": error.code}

        def stop_projects():
            cleanup_error = cleanup(projects, None)
            if cleanup_error:
                raise cleanup_error
            return {"volumes": "preserved"}

        if projects:
            try:
                check("cleanup", stop_projects)
            except Failure as problem:
                error = error or problem
    if error is None:
        report["status"] = "PASS"
    elif mode != "windows" or engine is not None:
        report["status"] = "FAIL"
    write_report(report_dir, report, sensitive)
    print("Compose acceptance:", report["status"])
    if error:
        print("Diagnostic:", error.code)
    return 0 if report["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--linux-ci", action="store_true")
    mode.add_argument("--linux", action="store_true")
    mode.add_argument("--windows", action="store_true")
    mode.add_argument("--verify-report", type=Path)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--context")
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    try:
        require(re.fullmatch(SHA, args.expected_sha), "checkout")
        if args.verify_report:
            require(args.verify_report.stat().st_size <= 128 * 1024, "report")
            report = json.loads(args.verify_report.read_text(encoding="utf-8"))
            validate_report(report, args.expected_sha)
            print(report["platform"], report["sha"], report["status"])
            return 0 if report["status"] == "PASS" else 1
        if not args.context or not args.report_dir:
            parser.error("--context and --report-dir are required for a run")
        if args.windows:
            allowed = ROOT / "local-runs" / "kt2-pr3"
            require(args.report_dir.resolve().is_relative_to(allowed.resolve()), "report")
        report_dir = args.report_dir.resolve()
        report_dir.mkdir(parents=True, exist_ok=False)
        return perform("windows" if args.windows else "linux-ci" if args.linux_ci else "linux",
                       args.expected_sha, args.context, report_dir)
    except (OSError, TypeError, ValueError, Failure) as error:
        print("Acceptance error:", error.code if isinstance(error, Failure) else "report")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
