import hashlib
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

    def planning_package(self):
        root = "openspec/changes/sample"
        self.planning_manifest = "openspec/verification/sample/planning.json"
        self.decisions = "docs/development/sample-business-decisions.md"
        repo = os.environ.get("GITHUB_REPOSITORY", "example/sample")
        issue = f"https://github.com/{repo}/issues/5"
        self.planning_files = {
            self.decisions: f"# Decisions\n\n[Issue]({issue})\n",
            f"{root}/.openspec.yaml": "schema: spec-driven\n",
            f"{root}/proposal.md": (f"# Proposal for sample\n\n[Issue]({issue})\n"
                                    f"[Decisions](../../../{self.decisions})\n"),
            f"{root}/design.md": "# Design\n\nThe service responds.\n",
            DELTA: "## ADDED Requirements\n### Requirement: AC-1 Example\nThe app SHALL respond.\n"
                   "#### Scenario: SC-1 Example\n- **WHEN** called\n- **THEN** responds\n"
                   "#### Scenario: SC-2 Example\n- **WHEN** called again\n- **THEN** responds\n",
            TASKS: "# Tasks\n\n## 1. Example\n\n"
                   "- [ ] 1.1 First. После: нет. AC-1 · SC-1.\n"
                   "- [ ] 1.2 Follow-up. После: 1.1. AC-1 · SC-2.\n"
                   "- [ ] 1.3 Regression AC-1 · SC-1–2. После: 1.1, 1.2.\n",
        }
        for path, text in self.planning_files.items():
            self.write(path, text)
        self.record = {
            "change_id": "sample", "issue": 5, "kind": "planning", "decisions": self.decisions,
            "scope": [*self.planning_files, self.planning_manifest], "tasks": [], "targets": {}, "links": [],
        }

    def commit_planning(self):
        self.git("add", "--all")
        self.record.setdefault("artifacts", {
            path: hashlib.sha256(subprocess.check_output(["git", "show", f":{path}"])).hexdigest()
            for path in self.planning_files if Path(path).is_file()
        })
        self.write(self.planning_manifest, json.dumps(self.record))
        return self.commit()

    def verify_planning(self):
        return sdd.check(self.base, self.commit_planning(), 5, kt1=self.base)

    def accept_planning_then_implementation(self):
        self.planning_package()
        self.planning_base = self.commit_planning()
        self.write(TASKS, self.planning_files[TASKS].replace("[ ] 1.1", "[x] 1.1"))
        self.record = {
            "change_id": "sample", "issue": 5, "kind": "implementation",
            "scope": [MANIFEST, TASKS], "tasks": ["1.1"],
            "targets": {"a": {"requirement": "AC-1", "scenario": "SC-1", "spec": DELTA}},
            "links": [{"task": "1.1", "target": "a", "test": TEST + "::test_example"}],
        }

    def verify_after_planning(self):
        self.write(MANIFEST, json.dumps(self.record))
        return sdd.check(self.planning_base, self.commit(), 5, kt1=self.base)

    def test_planning_package_passes_without_product_evidence(self):
        self.planning_package()
        self.assertEqual(self.verify_planning(), ("sample", 0, "planning"))

    def test_planning_rejects_product_or_ci_change_even_in_declared_scope(self):
        self.planning_package()
        for path in (TEST, ".github/workflows/extra.yml", "backend/migrations/001.py"):
            with self.subTest(path=path):
                self.record["scope"].append(path)
                original = Path(path).read_bytes() if Path(path).exists() else None
                self.write(path, "foreign change\n")
                with self.assertRaisesRegex(ValueError, "exactly one new complete package"):
                    self.verify_planning()
                self.record["scope"].remove(path)
                if original is None:
                    Path(path).unlink()
                else:
                    Path(path).write_bytes(original)

    def test_planning_rejects_existing_spec_archive_delete_and_rename(self):
        active = "openspec/specs/example/spec.md"
        archive = "openspec/changes/archive/2026-01-01-other/specs/example/spec.md"
        Path(TASKS).unlink()
        for path in (active, archive):
            self.write(path, "existing\n")
        self.base = self.commit()
        self.planning_package()
        for path, operation, error in (
            (active, "edit", "exactly one new complete package"),
            (archive, "edit", "Existing archived artifacts are immutable"),
            (active, "delete", "exactly one new complete package"),
            (TEST, "rename", "exactly one new complete package"),
        ):
            with self.subTest(path=path, operation=operation):
                target = Path(path)
                original = target.read_bytes()
                other = target.with_name("renamed.md")
                if operation == "edit":
                    self.write(path, "changed\n")
                elif operation == "delete":
                    target.unlink()
                else:
                    target.rename(other)
                    self.record["scope"].append(other.as_posix())
                self.record["scope"].append(path)
                with self.assertRaisesRegex(ValueError, error):
                    self.verify_planning()
                self.record["scope"].remove(path)
                if operation == "rename":
                    other.unlink()
                    self.record["scope"].remove(other.as_posix())
                target.write_bytes(original)

    def test_planning_rejects_wrong_id_issue_or_decisions_link(self):
        self.planning_package()
        for field, value in (("change_id", "other"), ("issue", 6)):
            with self.subTest(field=field):
                old = self.record[field]
                self.record[field] = value
                with self.assertRaisesRegex(ValueError, "Change ID or GitHub Issue"):
                    self.verify_planning()
                self.record[field] = old
        self.write(self.decisions, "# Unrelated\n")
        self.record.pop("artifacts")
        with self.assertRaisesRegex(ValueError, "decisions must be linked"):
            self.verify_planning()

    def test_planning_rejects_wrong_hash_and_incomplete_package(self):
        self.planning_package()
        self.commit_planning()
        self.record["artifacts"][DELTA] = self.git("hash-object", DELTA)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.verify_planning()
        self.record.pop("artifacts")
        Path("openspec/changes/sample/design.md").unlink()
        with self.assertRaisesRegex(ValueError, "complete package"):
            self.verify_planning()

    def test_planning_rejects_second_manifest(self):
        self.planning_package()
        self.write(MANIFEST, "{}")
        with self.assertRaisesRegex(ValueError, "Exactly one new or updated"):
            self.verify_planning()

    def test_planning_rejects_invalid_task_trace_and_dependencies(self):
        self.planning_package()
        original = self.planning_files[TASKS]
        for token, replacement, error in (
            ("[ ] 1.1", "[x] 1.1", "unique, unchecked"),
            ("[ ] 1.2", "[ ] 1.1", "unique, unchecked"),
            ("SC-2.\n", "SC-99.\n", "Invalid AC/SC"),
            ("AC-1 · SC-2", "AC-2 · SC-2", "Invalid AC/SC"),
            ("После: 1.1.", "После: 1.9.", "Invalid dependency"),
            ("После: 1.1.", "После: 1.2.", "Invalid dependency"),
            ("После: 1.1.", "После: 1.3.", "Invalid dependency"),
            ("После: 1.1.", "Depends on 1.1.", "Missing dependencies"),
        ):
            with self.subTest(replacement=replacement):
                self.write(TASKS, original.replace(token, replacement))
                self.record.pop("artifacts", None)
                with self.assertRaisesRegex(ValueError, error):
                    self.verify_planning()

    def test_planning_rejects_duplicate_or_uncovered_spec_ids(self):
        self.planning_package()
        original = self.planning_files[DELTA]
        for delta, error in (
            (original + original, "Duplicate requirement"),
            (original + original.replace("AC-1", "AC-2"), "duplicate planning scenario"),
            (original + "#### Scenario: SC-3 Example\n- **WHEN** called\n- **THEN** responds\n",
             "trace every scenario"),
        ):
            with self.subTest(error=error):
                self.write(DELTA, delta)
                self.record.pop("artifacts", None)
                with self.assertRaisesRegex(ValueError, error):
                    self.verify_planning()

    def test_planning_rejects_implementation_coverage_claims(self):
        self.planning_package()
        for field, claim in (("tasks", ["1.1"]), ("targets", {"covered": {}}),
                             ("links", [{"test": TEST}]), ("coverage", "PASS")):
            with self.subTest(field=field):
                original = self.record.get(field)
                self.record[field] = claim
                with self.assertRaisesRegex(ValueError, "cannot claim implementation"):
                    self.verify_planning()
                if original is None:
                    del self.record[field]
                else:
                    self.record[field] = original

    def test_planning_rejects_mode_change_and_reuse(self):
        self.planning_package()
        accepted = self.commit_planning()
        self.base = accepted
        self.record["artifacts"][DELTA] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Accepted planning manifest is immutable"):
            self.verify_planning()

    def test_planning_rejects_numeric_self_dependency(self):
        self.planning_package()
        original = self.planning_files[TASKS]
        self.write(TASKS, original.replace("1.1 First. После: нет.", "1 First. После: 1."))
        with self.assertRaisesRegex(ValueError, "Invalid dependency"):
            self.verify_planning()

    def test_planning_rejects_archived_change_id(self):
        Path(TASKS).unlink()
        self.write("openspec/changes/archive/2026-01-01-sample/tasks.md", "# Archived\n")
        self.base = self.commit()
        self.planning_package()
        with self.assertRaisesRegex(ValueError, "cannot reuse an archived change ID"):
            self.verify_planning()

    def test_implementation_after_planning_only_maps_its_slice(self):
        self.accept_planning_then_implementation()
        self.assertEqual(self.verify_after_planning(), ("sample", 1, "implementation"))

    def test_implementation_after_planning_still_requires_evidence(self):
        self.accept_planning_then_implementation()
        self.record["links"] = []
        with self.assertRaisesRegex(ValueError, "Explicit verification links"):
            self.verify_after_planning()

    def test_implementation_cannot_change_approved_delta(self):
        self.accept_planning_then_implementation()
        self.record["scope"].append(DELTA)
        self.write(DELTA, self.planning_files[DELTA].replace("respond.", "stop."))
        with self.assertRaisesRegex(ValueError, "Approved planning artifacts are immutable"):
            self.verify_after_planning()

    def test_implementation_cannot_change_approved_proposal_or_decisions(self):
        self.accept_planning_then_implementation()
        proposal = "openspec/changes/sample/proposal.md"
        for path, error in ((proposal, "Approved planning artifact"), (self.decisions, "Approved business decisions")):
            with self.subTest(path=path):
                self.record["scope"].append(path)
                self.write(path, self.planning_files[path] + "\nChanged contract\n")
                with self.assertRaisesRegex(ValueError, error):
                    self.verify_after_planning()
                self.write(path, self.planning_files[path])
                self.record["scope"].remove(path)

    def test_implementation_rejects_wrong_issue_for_approved_plan(self):
        self.accept_planning_then_implementation()
        self.record["issue"] = 6
        self.write(MANIFEST, json.dumps(self.record))
        with self.assertRaisesRegex(ValueError, "must match the approved planning Issue"):
            sdd.check(self.planning_base, self.commit(), 6, kt1=self.base)

    def test_implementation_rejects_existing_but_wrong_scenario_for_task(self):
        self.accept_planning_then_implementation()
        self.record["targets"]["a"]["scenario"] = "SC-2"
        with self.assertRaisesRegex(ValueError, "outside approved planning trace"):
            self.verify_after_planning()

    def test_implementation_rejects_unapproved_tasks_and_unclaimed_completion(self):
        self.accept_planning_then_implementation()
        checked = self.planning_files[TASKS].replace("[ ] 1.1", "[x] 1.1")
        self.write(TASKS, checked + "- [ ] 9.9 New. После: 1.1. AC-1 · SC-1.\n")
        self.record["tasks"].append("9.9")
        self.record["links"].append({"task": "9.9", "target": "a", "test": TEST + "::test_example"})
        with self.assertRaisesRegex(ValueError, "must preserve the approved planning task rows"):
            self.verify_after_planning()
        self.record["tasks"].remove("9.9")
        self.record["links"].pop()
        self.write(TASKS, checked.replace("[ ] 1.2", "[x] 1.2"))
        with self.assertRaisesRegex(ValueError, "outside the declared slice"):
            self.verify_after_planning()

    def archive_planned_change(self):
        self.accept_planning_then_implementation()
        root = "openspec/changes/sample/"
        archive = "openspec/changes/archive/2026-01-01-sample/"
        self.record["scope"] = [MANIFEST]
        for source in (path for path in self.planning_files if path.startswith(root)):
            target = Path(archive + source.removeprefix(root))
            target.parent.mkdir(parents=True, exist_ok=True)
            Path(source).rename(target)
            self.record["scope"] += [source, target.as_posix()]
        self.write(archive + "tasks.md", self.planning_files[TASKS].replace("[ ]", "[x]"))
        self.record["tasks"] = ["1.1", "1.2", "1.3"]
        self.record["targets"]["b"] = {"requirement": "AC-1", "scenario": "SC-2", "spec": DELTA}
        for task, target in (("1.2", "b"), ("1.3", "a")):
            self.record["links"].append({"task": task, "target": target, "test": TEST + "::test_example"})
        return root, archive

    def test_implementation_can_archive_approved_delta_unchanged(self):
        root, archive = self.archive_planned_change()
        self.assertEqual(self.verify_after_planning(), ("sample", 3, "implementation"))
        self.write(archive + DELTA.removeprefix(root), self.planning_files[DELTA] + "\n")
        with self.assertRaisesRegex(ValueError, "can only be archived unchanged"):
            self.verify_after_planning()

    def test_implementation_rejects_split_archive(self):
        _, archive = self.archive_planned_change()
        proposal = archive + "proposal.md"
        other = Path(proposal.replace("2026-01-01", "2026-01-02"))
        other.parent.mkdir(parents=True, exist_ok=True)
        Path(proposal).rename(other)
        self.record["scope"].remove(proposal)
        self.record["scope"].append(other.as_posix())
        with self.assertRaisesRegex(ValueError, "must share one archive directory"):
            self.verify_after_planning()

    def test_implementation_cannot_archive_only_tasks(self):
        self.accept_planning_then_implementation()
        target = "openspec/changes/archive/2026-01-01-sample/tasks.md"
        Path(TASKS).unlink()
        self.write(target, self.planning_files[TASKS].replace("[ ]", "[x]"))
        self.record["scope"].append(target)
        with self.assertRaisesRegex(ValueError, "immutable outside a complete archive"):
            self.verify_after_planning()

    def test_tooling_cannot_add_a_new_specification(self):
        self.add_delta()
        self.record.update(kind="tooling", reason="Maintenance", targets={})
        self.record["links"] = [{"task": "1.1", "test": TEST + "::test_example"}]
        with self.assertRaisesRegex(ValueError, "cannot contain product code or specs"):
            self.verify()

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

    def test_nested_delta_requires_target(self):
        nested = DELTA.replace("/example/", "/identity/user-auth/")
        self.add_delta()
        self.record["scope"][-1] = nested
        (Path(nested).parent).mkdir(parents=True)
        Path(DELTA).rename(nested)
        with self.assertRaisesRegex(ValueError, "Changed spec missing a verification target"):
            self.verify()

    def test_nested_sync_requires_all_markdown_tasks_complete(self):
        spec = "openspec/specs/identity/user-auth/spec.md"
        self.record["scope"].append(spec)
        self.write(spec, "## Requirements\n### Requirement: AC-1 Example\n")
        for bullet in ("*", "+", "2.", "2)"):
            with self.subTest(bullet=bullet), self.assertRaisesRegex(
                ValueError, "Cannot sync or archive with incomplete tasks"
            ):
                self.write(TASKS, f"- [x] 1.1 Done\n{bullet} [ ] 2.1 Still open\n")
                self.verify()

    def test_removed_list_entry_can_be_mapped(self):
        self.record["scope"].append(DELTA)
        self.write(DELTA, "\ufeff## removed Requirements ##\n* `### Requirement: AC-1 Old`\n")
        self.record["targets"]["a"] = {"requirement": "AC-1", "spec": DELTA}
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))

    def test_scenario_heading_with_lowercase_prefix_requires_target(self):
        self.add_delta()
        self.write(DELTA, Path(DELTA).read_text(encoding="utf-8").replace("Scenario:", "scenario:"))
        self.record["targets"]["a"] = {"requirement": "AC-1", "spec": DELTA}
        with self.assertRaisesRegex(ValueError, "Unknown spec requirement/scenario"):
            self.verify()

    def test_unlabelled_scenario_and_fenced_example(self):
        self.record["scope"].append(DELTA)
        self.write(DELTA, "## ADDED Requirements\n### Requirement: AC-1 Example\n"
                   "```markdown\n#### Scenario: SC-FAKE Example\n```\n#### SC-1 Example\n"
                   "- **WHEN** called\n- **THEN** responds\n")
        self.record["targets"]["a"] = {"requirement": "AC-1", "scenario": "SC-1", "spec": DELTA}
        self.assertEqual(self.verify(), ("sample", 1, "implementation"))
