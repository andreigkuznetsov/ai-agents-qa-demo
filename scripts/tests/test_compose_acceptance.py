import copy
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "acceptance", Path(__file__).resolve().parents[1] / "compose-acceptance.py"
)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class AcceptanceTests(unittest.TestCase):
    def test_guards(self):
        self.assertTrue(a.local_endpoint("linux", "unix:///var/run/docker.sock"))
        self.assertTrue(a.local_endpoint("windows", "npipe:////./pipe/dockerDesktopLinuxEngine"))
        for endpoint in ("ssh://remote", "tcp://127.0.0.1:2375", "unix:///tmp/proxy"):
            self.assertFalse(a.local_endpoint("linux", endpoint))
        for key in ("DOCKER_HOST", "DOCKER_CONFIG", "COMPOSE_FILE", "QA_HTTP_PORT", "BUILDX_BUILDER"):
            with self.assertRaises(a.Failure):
                a.check_environment({key: "external"}, "default")

    def test_ownership(self):
        a.owned({"com.docker.compose.project": "project",
                 "com.docker.compose.volume": "postgres"}, "project", "volume", "postgres")
        with self.assertRaises(a.Failure):
            a.owned({"com.docker.compose.project": "other"}, "project", "volume", "postgres")
        with self.assertRaises(a.Failure):
            a.same({"Id": "second"}, {"Id": "first"})
        with self.assertRaises(a.Failure):
            a.same({"CreatedAt": "second"}, {"CreatedAt": "first"})

    def test_collision_prevents_environment_file_creation(self):
        engine = Mock()
        engine.run.return_value = "claimed"
        with patch.object(Path, "open") as open_file:
            with self.assertRaisesRegex(a.Failure, "^collision$"):
                a.Project(engine, Path("not-created"), "1" * 24, "positive", "image", [])
        open_file.assert_not_called()

    def test_build_selects_engine_default_builder(self):
        project = a.Project.__new__(a.Project)
        project.engine = Mock()
        project.compose = Mock()
        project.build()
        project.engine.ensure.assert_called_once()
        project.compose.assert_called_once_with("build", "--builder", "default", "api", timeout=300)

    def test_bindings(self):
        observed = {"db": {"Bindings": {}, "Ports": {"5432/tcp": None}},
                    "api": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32768"}]}}}
        self.assertEqual(a.binding(observed), 32768)
        observed["db"]["Bindings"] = {"5432/tcp": [{"HostIp": "127.0.0.1"}]}
        with self.assertRaisesRegex(a.Failure, "^network$"):
            a.binding(observed)

    def test_deadline(self):
        with patch.object(a, "monotonic", return_value=5):
            self.assertEqual(a.remaining(12, 4), 4)
            self.assertEqual(a.remaining(8, 4), 3)
            with self.assertRaises(a.Failure):
                a.remaining(5, 4)

    def test_health_and_recovery(self):
        self.assertEqual(a.health_status(503, {"status": "unavailable"}, 3.1), 503)
        for status, body, elapsed in (
            (500, {"status": "unavailable"}, 0),
            (200, {"status": "unavailable"}, 0),
            (503, {"status": "unavailable", "detail": "internal"}, 0),
            (200, {"status": "ok"}, 3.51),
        ):
            with self.assertRaises(a.Failure):
                a.health_status(status, body, elapsed)

    def test_http_probe_uses_loopback_and_validates_response(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok"}'
        with patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response
            self.assertLessEqual(a.probe(32768, "/health/ready", 200), 3500)
        connection.assert_called_once_with("127.0.0.1", 32768, timeout=3.5)
        connection.return_value.request.assert_called_once_with("GET", "/health/ready")
        connection.return_value.close.assert_called_once()

    def test_startup_order(self):
        a.migration_order(
            {"ExitCode": 0, "FinishedAt": "2026-11-23T00:00:00.1Z"},
            {"StartedAt": "2026-11-23T00:00:00.2Z"},
        )
        with self.assertRaises(a.Failure):
            a.migration_order(
                {"ExitCode": 0, "FinishedAt": "2026-11-23T00:00:00.3Z"},
                {"StartedAt": "2026-11-23T00:00:00.2Z"},
            )

    def test_migration_failure(self):
        a.failed_migration(1, 1, True, True, False, False)
        for observed in ((0, 1, True, True, False, False),
                         (1, 1, True, True, True, False),
                         (1, 1, True, True, False, True),
                         (1, 1, True, False, False, False)):
            with self.assertRaises(a.Failure):
                a.failed_migration(*observed)

    def test_persistence(self):
        row = ["60c474a3-1332-43ce-a044-741f28e317ff", 1, "2026-11-23T00:00:00+00:00", "CREATED"]
        a.persisted(row, row.copy(), 1)
        for actual, count in ((row, 2), (row[:-1] + ["OTHER"], 1), (None, 1)):
            with self.assertRaises(a.Failure):
                a.persisted(row, actual, count)

    def test_report_validation(self):
        sha = "a" * 40
        report = a.make_report(sha, "linux-ci")
        a.validate_report(report, sha)
        report["status"] = "PASS"
        with self.assertRaises(a.Failure):
            a.validate_report(report, sha)
        report["versions"] = dict.fromkeys(a.VERSION_KEYS, "2.1.1")
        report["run"] = "https://github.com/andreigkuznetsov/ai-agents-qa-demo/actions/runs/123"
        report["projects"] = {p: f"kt2caf-{'1' * 24}-{p}" for p in ("positive", "negative")}
        report["checks"] = dict.fromkeys(a.REQUIRED["linux-ci"], {})
        for check in report["checks"]:
            report["checks"][check] = a.check_entry(check, a.monotonic())
        time = "2026-11-23T00:00:10Z"
        volume = dict(volume_name=report["projects"]["positive"] + "_postgres",
                      volume_created=time, image="sha256:" + "a" * 64)
        api = dict(api_id="b" * 64, api_started=time)
        facts = {
            "preflight": dict(checkout="clean", engine="unix", version_source="docker"),
            "startup": dict(start_ms="1000", revision="0001_runs", migration_exit="0",
                            migrate_before_api="yes", binding="127.0.0.1:12345",
                            migration_finished=time, api_started=time, postgres="18.7",
                            postgres_port="unpublished", live="200 ok", ready="200 ok",
                            healthz="200 ok", live_ms="5", ready_ms="5", healthz_ms="5", **volume),
            "stop": dict(volume="preserved", api="stopped", db="stopped"),
            "persistence": dict(run_id="60c474a3-1332-43ce-a044-741f28e317ff",
                                schema_version="1", created_at="2026-11-23T00:00:10+00:00",
                                status="CREATED", count="1", start_ms="1000", migration_exit="0",
                                revision="0001_runs", ready="200 ok", **volume),
            "database-loss": dict(live="200 ok", ready="503 unavailable",
                                  healthz="503 unavailable", ready_ms="100", healthz_ms="20", **api),
            "database-recovery": dict(ready="200 ok", healthz="200 ok", api="same", ready_ms="12",
                                      healthz_ms="10", **api),
            "migration-failure": dict(migration_exit="1", diagnostic="migration-configuration",
                                      api="never-started", tables="absent"),
            "cleanup": dict(volumes="preserved"),
        }
        for check, evidence in facts.items():
            report["checks"][check]["facts"] = evidence
        a.validate_report(report, sha)
        for mutation in (
            lambda r: r.update({"sha": "b" * 40}),
            lambda r: r.update({"logs": "password=example"}),
            lambda r: r["versions"].update({"engine": "postgresql://test:secret@db"}),
            lambda r: r["checks"].pop("migration-failure"),
            lambda r: r.update({"run": ""}),
            lambda r: r["checks"]["persistence"]["facts"].update(volume_created="2026-11-23T00:00:11Z"),
            lambda r: r["checks"]["database-recovery"]["facts"].update(api_id="c" * 64),
            lambda r: r["checks"]["database-recovery"]["facts"].update(ready_ms="3501"),
        ):
            invalid = copy.deepcopy(report)
            mutation(invalid)
            with self.assertRaises(a.Failure):
                a.validate_report(invalid, sha)

    def test_windows_unverified_is_not_pass(self):
        sha = "a" * 40
        report = a.make_report(sha, "windows")
        self.assertEqual(report["status"], "NOT VERIFIED")
        a.validate_report(report, sha)
        report["status"] = "PASS"
        with self.assertRaises(a.Failure):
            a.validate_report(report, sha)

    def test_local_linux_success_is_not_acceptance(self):
        self.assertEqual(a.successful_status("linux-local"), "SMOKE PASS")
        sha = "a" * 40
        report = a.make_report(sha, "linux-local")
        report["status"] = "PASS"
        with self.assertRaises(a.Failure):
            a.validate_report(report, sha)

    def test_cleanup_preserves_first_failure(self):
        failure = a.Failure("command-failed")
        project = Mock()
        project.stop.side_effect = a.Failure("ownership")
        self.assertIs(a.cleanup([project], failure), failure)
        self.assertEqual(a.cleanup([project], None).code, "ownership")

    def test_command_timeout_discards_output(self):
        with patch.object(a.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ["docker"], 1, output=b"raw-sensitive-data"
        )):
            with self.assertRaisesRegex(a.Failure, "^timeout$"):
                a.invoke(["docker"], 1, {})
