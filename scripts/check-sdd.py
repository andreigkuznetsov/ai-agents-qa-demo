"""Check SDD declarations in a PR's committed Git trees, without running PR tests."""

import argparse
import ast
import json
from pathlib import PurePosixPath
import re
import subprocess
import sys


KT1 = "7c2d939cec68fa39eae9f3dfb84e9f055404810c"
MANIFEST = re.compile(r"openspec/verification/([a-z0-9-]+)/[a-z0-9-]+\.json")
SPEC = re.compile(r"openspec/(?:specs|changes/(?!archive/)[^/]+/specs)/[^/]+/spec\.md")
TASK = re.compile(r"^\s*- \[[ xX]\] ([0-9]+(?:\.[0-9]+)*) ", re.M)
OPEN_TASK = re.compile(r"^\s*- \[ \] ", re.M)
REQUIREMENT = re.compile(r"^### Requirement: (\S+)[^\n]*", re.M)
SCENARIO = re.compile(r"^#### Scenario: (\S+)[^\n]*", re.M)
RENAME_SECTION = re.compile(r"^## RENAMED Requirements[ \t]*\n([^\n]*(?:\n(?!## )[^\n]*)*)", re.M)
RENAME_LINE = re.compile(r"^[ \t]*- (FROM|TO):[ \t]*(.+?)[ \t]*$", re.M)


def require(valid, message):
    if not valid:
        raise ValueError(message)


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8")


def paths(*args):
    return set(git(*args).strip("\0").split("\0")) - {""}


def repo_path(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._/-]+", value)
            and all(part not in {"", ".", ".."} for part in value.split("/")),
            f"Invalid repository path: {value!r}")
    return value


def unique_strings(values, label):
    require(isinstance(values, list) and values
            and all(isinstance(item, str) and item for item in values), f"Empty or invalid {label}")
    require(len(set(values)) == len(values), f"Duplicate {label}")
    return set(values)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def spec_blocks(text):
    headings = list(REQUIREMENT.finditer(text))
    blocks = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.start():end]
        # A delta section heading belongs to the next requirement.
        block = re.split(r"^## ", block, maxsplit=1, flags=re.M)[0].strip()
        key = heading[1]
        require(key not in blocks, f"Duplicate requirement ID in spec: {key}")
        scenarios = SCENARIO.findall(block)
        require(len(scenarios) == len(set(scenarios)), f"Duplicate scenario ID in {key}")
        blocks[key] = (block, set(scenarios))
    for section in RENAME_SECTION.findall(text):
        operations = RENAME_LINE.findall(section)
        require(operations and len(operations) % 2 == 0, "Expected FROM/TO pairs in RENAMED")
        seen = set()
        for (before, old), (after, new) in zip(operations[::2], operations[1::2]):
            previous_id = old.strip("`").removeprefix("### Requirement: ").split()[0]
            next_id = new.strip("`").removeprefix("### Requirement: ").split()[0]
            require(before == "FROM" and after == "TO" and previous_id == next_id,
                    "Rename must keep the requirement ID and use FROM followed by TO")
            require(previous_id not in seen, f"Duplicate rename of {previous_id}")
            seen.add(previous_id)
            block, scenarios = blocks.get(previous_id, ("", set()))
            blocks[previous_id] = (block + f"\nFROM: {old}\nTO: {new}", scenarios)
    return blocks


def test_exists(text, selector):
    parts = selector.split("::")
    if len(parts) not in (1, 2) or not parts[-1].startswith("test_"):
        return False
    nodes = ast.parse(text).body
    if len(parts) == 2:
        nodes = next((node.body for node in nodes if isinstance(node, ast.ClassDef)
                      and node.name == parts[0]), [])
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == parts[-1] for node in nodes)


def check(base, head, issue, kt1=KT1):
    base = git("merge-base", base, head).strip()
    approved = paths("ls-tree", "-r", "--name-only", "-z", kt1, "--", "docs", "prototype")
    protected = {p for p in approved if re.match(r"docs/(?:0[1-7]_|KT1_)|prototype/", p)}
    require(len(protected) == 10, "Expected ten paths in the normative KT1 baseline")
    require(not git("diff", "--name-only", kt1, head, "--", *sorted(protected)),
            "Normative KT1 files differ from the approved baseline")
    changed = paths("diff", "--name-only", "--no-renames", "-z", base, head, "--")
    old_files = paths("ls-tree", "-r", "--name-only", "-z", base)
    files = paths("ls-tree", "-r", "--name-only", "-z", head)

    def read(path, ref=head):
        return git("show", f"{ref}:{path}")

    manifests = sorted(p for p in changed & files if MANIFEST.fullmatch(p))
    require(len(manifests) == 1, "Exactly one new or updated SDD verification manifest is required")
    manifest_path = manifests[0]
    change_id = MANIFEST.fullmatch(manifest_path)[1]
    record = json.loads(read(manifest_path), object_pairs_hook=unique_object)
    require(record["change_id"] == change_id and type(record["issue"]) is int
            and record["issue"] == issue and issue > 0, "Change ID or GitHub Issue does not match")
    scope = {repo_path(path) for path in unique_strings(record["scope"], "scope")}
    require(not (changed - scope), "Outside approved scope: " + ", ".join(sorted(changed - scope)))
    tasks = unique_strings(record["tasks"], "tasks")
    targets = record["targets"]
    links = record["links"]
    require(isinstance(targets, dict) and isinstance(links, list) and links,
            "Explicit verification links are required")
    requirements = dict(re.findall(r"^\| (REQ-\d{3}) \|[^\n]*\|([^|\n]+)\|\s*$",
                                   read("docs/02_REQUIREMENTS.md"), re.M))
    scenarios = set(re.findall(r"^### (S0[1-6])\b", read("docs/03_SCENARIOS.md"), re.M))

    spec_paths = {p for p in changed if SPEC.fullmatch(p)}
    archive = {p for p in changed if p.startswith("openspec/changes/archive/") and not p.endswith("/.gitkeep")}
    require(not archive & old_files, "Existing archived artifacts are immutable")

    def owns_active(path):
        return not path.startswith("openspec/changes/") or path.startswith(
            (f"openspec/changes/{change_id}/", "openspec/changes/archive/")
        )

    require(all(owns_active(path) for path in changed),
            "Changed active artifacts must belong to this change")
    for path in sorted(changed & files):
        if path.startswith(("openspec/changes/", "openspec/specs/")) and path.endswith(".md"):
            text = read(path)
            for ref in set(re.findall(r"\bREQ-\d{3}\b|\bS\d{2}\b", text)):
                require(ref in requirements or ref in scenarios, f"Unknown KT1 reference {ref} in {path}")
    if record["kind"] == "tooling":
        require(isinstance(record.get("reason"), str) and record["reason"].strip()
                and not targets and not spec_paths and not archive
                and all(p in {"AGENTS.md", "README.md", ".gitignore"}
                        or p.startswith((".agents/", ".github/", "docs/development/", "openspec/", "scripts/"))
                        for p in changed),
                "Tooling exception needs a reason and cannot contain product code or specs")
    else:
        require(record["kind"] == "implementation" and targets,
                "Implementation requires explicit requirement/scenario targets")
        task_paths = sorted(p for p in files if p == f"openspec/changes/{change_id}/tasks.md"
                            or re.fullmatch(rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-"
                                            rf"{re.escape(change_id)}/tasks\.md", p))
        require(len(task_paths) == 1, "Expected one active or archived tasks.md for this change")
        task_text = read(task_paths[0])
        declared = TASK.findall(task_text)
        require(len(declared) == len(set(declared)) and tasks <= set(declared),
                "Slice tasks must refer to unique task IDs in tasks.md")
        if archive or any(spec.startswith("openspec/specs/") for spec in spec_paths):
            require(not OPEN_TASK.search(task_text), "Cannot sync or archive with incomplete tasks")
        require(all(re.fullmatch(rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-"
                                 rf"{re.escape(change_id)}/.+", p) for p in archive),
                "Archive paths must belong to this change")

    mapped = set()
    for target in targets.values():
        requirement, scenario = target["requirement"], target.get("scenario")
        spec = target.get("spec")
        if requirement.startswith("REQ-"):
            require(requirement in requirements, f"Unknown KT1 requirement: {requirement}")
        if scenario and scenario.startswith("S") and not scenario.startswith("SC-"):
            require(scenario in scenarios, f"Unknown KT1 scenario: {scenario}")
            if requirement in requirements:
                refs = requirements[requirement]
                require(scenario in refs or ("S01–S06" in refs and scenario in scenarios),
                        f"{requirement} does not reference {scenario} in KT1")
        if spec is None:
            require(requirement in requirements and (scenario is None or scenario in scenarios),
                    "A non-KT1 target needs a spec path")
            continue
        repo_path(spec)
        require(owns_active(spec), f"Spec target must belong to this change: {spec}")
        require(SPEC.fullmatch(spec) and (spec in files or spec in old_files),
                f"Unknown spec path: {spec}")
        blocks = [spec_blocks(read(spec, ref)).get(requirement)
                  for ref, tree in ((head, files), (base, old_files)) if spec in tree]
        require(any(block and scenario in (block[1] or {None}) for block in blocks),
                f"Unknown spec requirement/scenario: {requirement}/{scenario} in {spec}")
        mapped.add((spec, requirement, scenario))

    for spec in sorted(spec_paths):
        before = spec_blocks(read(spec, base)) if spec in old_files else {}
        after = spec_blocks(read(spec)) if spec in files else {}
        if spec not in files and spec.startswith(f"openspec/changes/{change_id}/"):
            suffix = spec.removeprefix(f"openspec/changes/{change_id}/")
            archived = [p for p in changed & files if re.fullmatch(
                rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(change_id)}/"
                + re.escape(suffix), p)]
            if len(archived) == 1 and read(archived[0]) == read(spec, base):
                continue
        require(before or after, f"No requirement headings in modified spec: {spec}")
        for req in before.keys() | after.keys():
            if before.get(req) != after.get(req):
                names = before.get(req, ("", set()))[1] | after.get(req, ("", set()))[1]
                for scenario in names or {None}:
                    require((spec, req, scenario) in mapped,
                            f"Changed spec missing a verification target: {spec} {req}/{scenario}")

    covered_tasks = set()
    covered_targets = set()
    for link in links:
        task, target = link["task"], link.get("target")
        require(task in tasks and (target in targets if targets else target is None),
                f"Unknown or missing task/target in verification link: {task}/{target}")
        test, manual = link.get("test"), link.get("manual")
        require((test is None) != (manual is None), "A link needs exactly one test or manual record")
        if test is not None:
            path, sep, selector = test.partition("::")
            repo_path(path)
            require(path in files and PurePosixPath(path).match("test_*.py")
                    and sep and test_exists(read(path), selector), f"Test not found: {test}")
        else:
            repo_path(manual)
            require(manual in files and manual.endswith(".md"),
                    f"Manual verification record not found: {manual}")
            evidence = read(manual)
            require(all(re.search(rf"^{field}:[ \t]+\S.*$", evidence, re.M) for field in ("Command", "Evidence"))
                    and re.search(r"^Result:[ \t]+PASS[ \t]*$", evidence, re.M),
                    f"Manual verification needs Command, Result: PASS, and Evidence: {manual}")
        covered_tasks.add(task)
        if target is not None:
            covered_targets.add(target)
    require(tasks == covered_tasks and set(targets) == covered_targets,
            "Every task and declared requirement/scenario needs a verification link")
    return change_id, len(links), record["kind"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--issue", type=int, required=True)
    args = parser.parse_args()
    try:
        change, links, kind = check(args.base, args.head, args.issue)
    except (ValueError, LookupError, TypeError, AttributeError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"SDD check failed: {error}", file=sys.stderr)
        return 1
    print(f"SDD declarations OK: {change} ({kind}, {links} links). Test execution is separate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
