import importlib.util
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


MODULE = importlib.util.spec_from_file_location(
    "check_sdd", Path(__file__).resolve().parents[1] / "check-sdd.py"
)
assert MODULE and MODULE.loader
sdd = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(sdd)

MANIFEST = "openspec/verification/sample/first.json"
TASKS = "openspec/changes/sample/tasks.md"
DELTA = "openspec/changes/sample/specs/example/spec.md"
TEST = "backend/tests/test_example.py"


class SDDTests(unittest.TestCase):
    def setUp(self):
        original = Path.cwd()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(os.chdir, original)
        os.chdir(temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "SDD fixture")
        self.git("config", "user.email", "ci@example.invalid")
        for index in range(1, 8):
            self.write(f"docs/0{index}_fixture.md", "immutable\n")
        self.write("docs/KT1_BASELINE.md", "immutable\n")
        self.write("docs/KT1_PACKAGE_README.md", "immutable\n")
        self.write("prototype/index.html", "immutable\n")
        Path("docs/02_fixture.md").rename("docs/02_REQUIREMENTS.md")
        Path("docs/03_fixture.md").rename("docs/03_SCENARIOS.md")
        self.write("docs/02_REQUIREMENTS.md", "| REQ-001 | Example | S01–S06 |\n")
        self.write("docs/03_SCENARIOS.md", "### S01 — Example\n### S06 — Example\n")
        self.write(TEST, "def test_example():\n    assert True\n")
        self.base = self.commit()
        self.write(TASKS, "- [ ] 1.1 Example\n")
        self.record = {
            "change_id": "sample",
            "issue": 5,
            "kind": "implementation",
            "scope": [MANIFEST, TASKS],
            "tasks": ["1.1"],
            "targets": {"a": {"requirement": "REQ-001", "scenario": "S01"}},
            "links": [{"task": "1.1", "target": "a", "test": TEST + "::test_example"}],
        }

    def git(self, *args):
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self, path, content):
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def verify(self):
        self.write(MANIFEST, json.dumps(self.record))
        return sdd.check(self.base, self.commit(), 5, kt1=self.base)

    def add_delta(self):
        self.record["scope"].append(DELTA)
        self.write(DELTA, "## ADDED Requirements\n### Requirement: AC-1 Example\n"
                   "The app SHALL respond.\n#### Scenario: SC-1 Example\n"
                   "- **WHEN** called\n- **THEN** responds\n")

    def test_valid_slice_can_reference_existing_test(self):
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))

    def test_valid_delta_with_verification_target(self):
        self.add_delta()
        self.record["targets"]["a"] = {
            "requirement": "AC-1", "scenario": "SC-1", "spec": DELTA,
        }
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))

    def test_unknown_requirement(self):
        self.record["targets"]["a"]["requirement"] = "REQ-999"
        with self.assertRaisesRegex(ValueError, "Unknown KT1 requirement"):
            self.verify()

    def test_unknown_scenario(self):
        self.record["targets"]["a"]["scenario"] = "S07"
        with self.assertRaisesRegex(ValueError, "Unknown KT1 scenario"):
            self.verify()

    def test_missing_test_function(self):
        self.record["links"][0]["test"] = TEST + "::test_missing"
        with self.assertRaisesRegex(ValueError, "Test not found"):
            self.verify()

    def test_broken_task_link(self):
        self.record["links"][0]["task"] = "7.2"
        with self.assertRaisesRegex(ValueError, "Unknown or missing task"):
            self.verify()

    def test_declared_requirement_without_link(self):
        self.record["targets"]["b"] = {"requirement": "REQ-001", "scenario": "S06"}
        with self.assertRaisesRegex(ValueError, "needs a verification link"):
            self.verify()

    def test_missing_target_even_without_spec_changes(self):
        self.record["targets"] = {}
        with self.assertRaisesRegex(ValueError, "requires explicit requirement"):
            self.verify()

    def test_changed_spec_scenario_without_target(self):
        self.add_delta()
        with self.assertRaisesRegex(ValueError, "Changed spec missing a verification target"):
            self.verify()

    def test_outside_scope_and_rename(self):
        Path(TEST).rename("backend/tests/test_relocated.py")
        self.record["scope"].append("backend/tests/test_relocated.py")
        with self.assertRaisesRegex(ValueError, "Outside approved scope: " + TEST):
            self.verify()

    def test_kt1_edit_forbidden_even_inside_scope(self):
        self.write("prototype/index.html", "changed\n")
        self.record["scope"].append("prototype/index.html")
        with self.assertRaisesRegex(ValueError, "Normative KT1"):
            self.verify()

    def test_wrong_issue(self):
        self.record["issue"] = 6
        with self.assertRaisesRegex(ValueError, "GitHub Issue does not match"):
            self.verify()

    def test_missing_manifest_rejected(self):
        with self.assertRaisesRegex(ValueError, "verification manifest is required"):
            sdd.check(self.base, self.commit(), 5, kt1=self.base)

    def test_tooling_exception_rejects_product_change(self):
        self.record.update(kind="tooling", reason="Bootstrap", targets={})
        self.write("backend/app/unrelated.py", "some_change = True\n")
        self.record["scope"].append("backend/app/unrelated.py")
        with self.assertRaisesRegex(ValueError, "Tooling exception"):
            self.verify()

    def test_scope_cannot_escape_repo(self):
        self.record["scope"].append("../outside")
        with self.assertRaisesRegex(ValueError, "Invalid repository path"):
            self.verify()

    def test_unlisted_unknown_requirement_in_plan(self):
        self.write(TASKS, "- [ ] 1.1 Example (REQ-999)\n")
        with self.assertRaisesRegex(ValueError, "Unknown KT1 reference REQ-999"):
            self.verify()

    def test_main_spec_sync_rejected_with_incomplete_tasks(self):
        spec = "openspec/specs/example/spec.md"
        self.record["scope"].append(spec)
        self.write(spec, "### Requirement: AC-1 Example\n#### Scenario: SC-1 Example\n")
        with self.assertRaisesRegex(ValueError, "Cannot sync or archive with incomplete tasks"):
            self.verify()

    def test_other_change_delta_rejected_even_in_scope(self):
        other = DELTA.replace("/sample/", "/another/")
        self.record["scope"].append(other)
        self.write(other, "## ADDED Requirements\n### Requirement: AC-1 Example\n")
        with self.assertRaisesRegex(ValueError, "Changed active artifacts must belong"):
            self.verify()

    def test_other_change_spec_target_rejected(self):
        other = DELTA.replace("/sample/", "/another/")
        task_text = Path(TASKS).read_text(encoding="utf-8")
        Path(TASKS).unlink()
        self.write(other, "## ADDED Requirements\n### Requirement: AC-1 Example\n")
        self.base = self.commit()
        self.write(TASKS, task_text)
        self.record["targets"]["a"] = {"requirement": "AC-1", "spec": other}
        with self.assertRaisesRegex(ValueError, "Spec target must belong"):
            self.verify()

    def test_empty_manual_evidence_is_rejected(self):
        evidence = "docs/development/sample-proof.md"
        self.record["scope"].append(evidence)
        self.record["links"][0].pop("test")
        self.record["links"][0]["manual"] = evidence
        self.write(evidence, "Nothing has been checked.\n")
        with self.assertRaisesRegex(ValueError, "Manual verification needs"):
            self.verify()

    def test_structured_manual_evidence_is_accepted_as_a_reference(self):
        evidence = "docs/development/sample-proof.md"
        self.record["scope"].append(evidence)
        self.record["links"][0].pop("test")
        self.record["links"][0]["manual"] = evidence
        self.write(evidence, "Command: inspect output\nResult: PASS\nEvidence: output fixture\n")
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))

    def test_rename_alone_can_be_mapped(self):
        self.record["scope"].append(DELTA)
        self.write(DELTA, "## RENAMED Requirements\n- FROM: `### Requirement: AC-1 Example`\n"
                   "- TO: `### Requirement: AC-1 New title`\n")
        self.record["targets"]["a"] = {"requirement": "AC-1", "spec": DELTA}
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))

    def test_rename_in_mixed_delta_requires_own_target(self):
        self.add_delta()
        with Path(DELTA).open("a", encoding="utf-8") as file:
            file.write("\n## RENAMED Requirements\n- FROM: `### Requirement: AC-2 Old`\n"
                       "- TO: `### Requirement: AC-2 New`\n")
        self.record["targets"]["a"] = {
            "requirement": "AC-1", "scenario": "SC-1", "spec": DELTA,
        }
        with self.assertRaisesRegex(ValueError, "AC-2/None"):
            self.verify()

    def test_rename_cannot_change_requirement_id(self):
        self.record["scope"].append(DELTA)
        self.write(DELTA, "## RENAMED Requirements\n- FROM: `### Requirement: AC-1 Example`\n"
                   "- TO: `### Requirement: AC-2 New title`\n")
        with self.assertRaisesRegex(ValueError, "Rename must keep the requirement ID"):
            self.verify()
