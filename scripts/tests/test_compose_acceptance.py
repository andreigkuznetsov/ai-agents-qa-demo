import copy
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
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

    def test_http_probe_rejects_valid_response_after_recovery_deadline(self):
        response = Mock(status=200)
        response.read.return_value = b'{"status":"ok"}'
        with patch("http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response
            with patch.object(a, "monotonic", side_effect=(14.9, 14.9, 15.1)):
                with self.assertRaisesRegex(a.Failure, "^timeout$"):
                    a.probe(32768, "/health/ready", 200, deadline=15)
        connection.return_value.close.assert_called_once()

    def test_desktop_version_uses_operator_confirmation_only_after_lookup_failure(self):
        result = Mock(returncode=0, stdout="4.33.1.161083\n")
        with patch.object(a, "invoke", return_value=result):
            self.assertEqual(a.desktop_version({}, "4.99.0"), ("4.33.1.161083", "windows-file-version"))
        with patch.object(a, "invoke", side_effect=a.Failure("command-failed")):
            self.assertEqual(a.desktop_version({}, "4.33.1"), ("4.33.1", "operator-desktop-ui"))
            with self.assertRaises(a.Failure):
                a.desktop_version({}, None)

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
        with self.assertRaises(a.Failure):
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
                                revision="0001_runs", ready="200 ok", ready_ms="9", **volume),
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
            lambda r: r["checks"]["preflight"]["facts"].update(engine="desktop-linux"),
        ):
            invalid = copy.deepcopy(report)
            mutation(invalid)
            with self.assertRaises(a.Failure):
                a.validate_report(invalid, sha)

    def test_windows_unverified_is_not_pass(self):
        sha = "a" * 40
        report = a.make_report(sha, "windows")
        self.assertEqual(report["status"], "NOT VERIFIED")
        with self.assertRaises(a.Failure):
            a.validate_report(report, sha)
        report["checks"]["preflight"] = a.check_entry(
            "preflight", a.monotonic(), a.Failure("engine-unavailable"),
        )
        a.validate_report(report, sha)
        report["status"] = "PASS"
        with self.assertRaises(a.Failure):
            a.validate_report(report, sha)

    def test_only_structurally_valid_failed_report_with_clean_summary_may_upload(self):
        sha = "a" * 40
        report = a.make_report(sha, "linux-ci")
        report["checks"]["preflight"] = a.check_entry("preflight", a.monotonic(), a.Failure("context"))
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            a.write_report(path, report, [])
            args = ["compose-acceptance.py", "--expected-sha", sha, "--verify-report",
                    str(path / "report.json"), "--check-summary"]
            with patch.object(a.sys, "argv", args):
                self.assertEqual(a.main(), 1)
            with patch.object(a.sys, "argv", [*args, "--allow-failed-report"]):
                self.assertEqual(a.main(), 0)
                (path / "summary.md").write_text("raw diagnostic output", encoding="utf-8")
                self.assertEqual(a.main(), 1)
                (path / "summary.md").write_text(a.summary_for(report), encoding="utf-8")
                report["unexpected"] = "raw diagnostic output"
                (path / "report.json").write_text(json.dumps(report), encoding="utf-8")
                self.assertEqual(a.main(), 1)

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
        project.stop.side_effect = KeyError("bad evidence")
        self.assertIs(a.cleanup([project], failure), failure)
        self.assertEqual(a.cleanup([project], None).code, "ownership")

    def test_first_start_timeout_registers_only_expected_resources_for_cleanup(self):
        project = a.Project.__new__(a.Project)
        project.engine = Mock(env={})
        project.name = "kt2caf-" + "1" * 24 + "-positive"
        project.env_file = Path("isolated.env")
        project.assert_fresh = Mock()
        project.verify = Mock()
        project.started = False
        project.containers = {}
        timeout = a.Failure("timeout")
        with patch.object(a, "invoke", side_effect=timeout):
            with self.assertRaises(a.Failure) as error:
                project.start()
        self.assertIs(error.exception, timeout)
        project.verify.assert_called_once_with(("db", "migrate", "api"), created=True)
        self.assertTrue(project.started)

    def test_launch_error_survives_ownership_failure_during_capture(self):
        project = a.Project.__new__(a.Project)
        project.verify = Mock(side_effect=a.Failure("ownership"))
        error = a.Failure("timeout")
        with self.assertRaises(a.Failure) as actual:
            project.launch(("db",), Mock(side_effect=error))
        self.assertIs(actual.exception, error)
        project.verify.assert_called_once_with(("db",), created=True)

    def test_command_timeout_discards_output(self):
        with patch.object(a.subprocess, "run", side_effect=subprocess.TimeoutExpired(
            ["docker"], 1, output=b"raw-sensitive-data"
        )):
            with self.assertRaisesRegex(a.Failure, "^timeout$"):
                a.invoke(["docker"], 1, {})
