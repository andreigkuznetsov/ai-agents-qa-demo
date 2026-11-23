"""Check SDD declarations in a PR's committed Git trees, without running PR tests."""

import argparse
import ast
import hashlib
import json
import os
from pathlib import PurePosixPath
import posixpath
import re
import subprocess
import sys


KT1 = "7c2d939cec68fa39eae9f3dfb84e9f055404810c"
MANIFEST = re.compile(r"openspec/verification/([a-z0-9-]+)/[a-z0-9-]+\.json")
PLANNING = re.compile(r"openspec/verification/([a-z0-9-]+)/planning\.json")
DECISIONS = re.compile(r"docs/development/[a-z0-9-]+-business-decisions\.md")
SPEC = re.compile(r"openspec/(?:specs|changes/(?!archive/)[^/]+/specs)/(?:[^/]+/)+spec\.md")
TASK_LINE = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]*\[[ \t]*([^\]\s]?)[ \t]*\](?![([])[ \t]*(.*)$", re.M)
TASK_ID = re.compile(r"^([0-9]+(?:\.[0-9]+)*)[ \t]+")
SECTION = re.compile(r"^##[ \t]+([^\n]+)", re.M)
REQUIREMENT = re.compile(r"^###[ \t]*Requirement:[ \t]*(\S+)[^\n]*", re.M | re.I)
SCENARIO = re.compile(r"^####[ \t]+(?:Scenario:[ \t]*)?(\S+)[^\n]*", re.M | re.I)
REMOVED_LINE = re.compile(r"^[ \t]*[-*+][ \t]*`?###[ \t]*Requirement:[ \t]*(.+?)`?[ \t]*$", re.M)
RENAME_LINE = re.compile(r"^[ \t]*[-*+]?[ \t]*(FROM|TO):[ \t]*`?###[ \t]*Requirement:[ \t]*(.+?)`?[ \t]*$", re.M)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
PLAN_REFERENCE = re.compile(r"\b((?:AC|SC)-(?:[A-Z][A-Z0-9]*-)*)(\d+)(?:[–-](\d+))?\b")
PLAN_DEPENDENCIES = re.compile(r"(?:^|\s)После:\s+(.+?)\.(?:\s|$)")
ISSUE_LINK = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)(?![\w/])")


def require(valid, message):
    if not valid:
        raise ValueError(message)


def git_bytes(*args):
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout


def git(*args):
    return git_bytes(*args).decode("utf-8")


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


def markdown_lines(text):
    """Keep line offsets while masking headings and directives inside fences."""
    active = None
    for line in text.splitlines(keepends=True):
        fence = FENCE.match(line)
        if active is not None:
            if fence and fence[1].startswith(active) and not fence[2].strip():
                active = None
            yield re.sub(r"[^\n]", " ", line)
        elif fence:
            active = fence[1]
            yield re.sub(r"[^\n]", " ", line)
        else:
            yield line


def spec_blocks(text):
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    masked = "".join(markdown_lines(text))
    headings = list(REQUIREMENT.finditer(masked))
    blocks = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        start = heading.start()
        following = SECTION.search(masked, start, end)
        if following:
            end = following.start()
        block = text[start:end].strip()
        key = heading[1]
        require(key not in blocks, f"Duplicate requirement ID in spec: {key}")
        scenarios = SCENARIO.findall(masked[start:end])
        require(len(scenarios) == len(set(scenarios)), f"Duplicate scenario ID in {key}")
        blocks[key] = (block, set(scenarios))
    sections = list(SECTION.finditer(masked))
    for index, heading in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(masked)
        section = masked[heading.end():end]
        label = " ".join(re.sub(r"[ \t]+#+[ \t]*$", "", heading[1]).lower().split())
        if label == "removed requirements":
            for name in REMOVED_LINE.findall(section):
                key = name.split()[0]
                require(key not in blocks, f"Duplicate requirement ID in spec: {key}")
                blocks[key] = (f"REMOVE: {name}", set())
        if label != "renamed requirements":
            continue
        operations = RENAME_LINE.findall(section)
        require(operations and len(operations) % 2 == 0, "Expected FROM/TO pairs in RENAMED")
        seen = set()
        for (before, old), (after, new) in zip(operations[::2], operations[1::2]):
            previous_id = old.split()[0]
            next_id = new.split()[0]
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


def check_planning_trace(task_text, specs, *, allow_completed=False):
    scenario_owners = {}
    requirements = set()
    for path, spec in specs.items():
        for requirement, (_, scenarios) in spec_blocks(spec).items():
            require(re.fullmatch(r"AC-(?:[A-Z][A-Z0-9]*-)*\d+", requirement)
                    and requirement not in requirements and scenarios,
                    f"Invalid, duplicate or unscoped planning requirement: {requirement}")
            requirements.add(requirement)
            for scenario in scenarios:
                require(re.fullmatch(r"SC-(?:[A-Z][A-Z0-9]*-)*\d+", scenario)
                        and scenario not in scenario_owners,
                        f"Invalid or duplicate planning scenario: {scenario}")
                scenario_owners[scenario] = (path, requirement)
    require(requirements, "Planning requires at least one requirement and scenario")

    tasks = {}
    covered = set()
    text = "".join(markdown_lines(task_text.lstrip("\ufeff")))
    for marker, row in TASK_LINE.findall(text):
        task = TASK_ID.match(row)
        require(task and (not marker or allow_completed and marker.lower() == "x") and task[1] not in tasks,
                "Planning requires unique, unchecked, numbered tasks")
        refs = set()
        for prefix, start, end in PLAN_REFERENCE.findall(row):
            if end:
                require(len(start) == len(end) and 0 < int(end) - int(start) < len(scenario_owners) + len(requirements),
                        f"Invalid planning reference range in task {task[1]}")
            refs.update(f"{prefix}{value:0{len(start)}d}" for value in range(int(start), int(end or start) + 1))
        ac = {ref for ref in refs if ref.startswith("AC-")}
        sc = refs - ac
        require(sc and sc <= scenario_owners.keys() and ac == {scenario_owners[ref][1] for ref in sc},
                f"Invalid AC/SC trace in planning task {task[1]}")
        dependency = PLAN_DEPENDENCIES.search(row)
        require(dependency, f"Missing dependencies in planning task {task[1]}")
        dependencies = [value.strip() for value in dependency[1].split(",")]
        if dependency[1] == "нет" or (not tasks and not re.search(r"\b\d+(?:\.|\b)", dependency[1])):
            dependencies = []
        require(len(dependencies) == len(set(dependencies)) and set(dependencies) <= tasks.keys(),
                f"Invalid dependency in planning task {task[1]}")
        covered.update(sc)
        tasks[task[1]] = (row.rstrip(), bool(marker), {(*scenario_owners[ref], ref) for ref in sc})
    require(covered == scenario_owners.keys(), "Planning tasks must trace every scenario")
    return tasks


def check_planning(record, manifest, change_id, head, changed, old_files, files):
    require(set(record) == {"kind", "change_id", "issue", "scope", "tasks", "targets", "links",
                            "decisions", "artifacts"}
            and record["tasks"] == [] and record["targets"] == {} and record["links"] == [],
            "Planning cannot claim implementation tasks, targets, links or additional evidence")
    require(PLANNING.fullmatch(manifest), "Use planning.json for a planning manifest")
    root = f"openspec/changes/{change_id}/"
    decisions = repo_path(record["decisions"])
    require(DECISIONS.fullmatch(decisions), "Invalid planning decisions path")
    specs = {path for path in files if path.startswith(root) and SPEC.fullmatch(path)}
    artifacts = {root + name for name in (".openspec.yaml", "proposal.md", "design.md", "tasks.md")}
    artifacts.update(specs | {decisions})
    expected = artifacts | {manifest}
    require(specs and expected == changed == set(record["scope"])
            and expected <= files and not expected & old_files
            and not any(path.startswith(root) for path in old_files),
            "Planning requires exactly one new complete package with no other changes")
    archive_prefix = re.compile(rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(change_id)}/")
    require(not any(archive_prefix.match(path) for path in old_files),
            "Planning cannot reuse an archived change ID")
    require(isinstance(record["artifacts"], dict) and record["artifacts"].keys() == artifacts,
            "Planning SHA-256 entries must match every artifact")
    entries = git("ls-tree", "-r", "-z", head, "--", *sorted(expected)).rstrip("\0").split("\0")
    require(len(entries) == len(expected) and all(entry.startswith("100644 blob ") for entry in entries),
            "Planning paths must be ordinary non-executable files")

    texts = {}
    for path in sorted(artifacts):
        raw = git_bytes("cat-file", "blob", f"{head}:{path}")
        require(hashlib.sha256(raw).hexdigest() == record["artifacts"][path],
                f"Planning SHA-256 mismatch: {path}")
        texts[path] = raw.decode("utf-8")
        require(texts[path].strip(), f"Empty planning artifact: {path}")
    proposal = texts[root + "proposal.md"]
    issue = str(record["issue"])
    repo = os.environ.get("GITHUB_REPOSITORY", "").lower()
    issue_links = {m[0] for m in ISSUE_LINK.finditer(proposal)
                   if m[2] == issue and (not repo or m[1].lower() == repo)}
    decision_links = {m[0] for m in ISSUE_LINK.finditer(texts[decisions])}
    local_links = re.findall(r"\]\(([^)\s]+)\)", proposal)
    require(re.search(rf"(?<![\w-]){re.escape(change_id)}(?![\w-])", proposal)
            and issue_links & decision_links
            and decisions in {posixpath.normpath(root + link) for link in local_links},
            "Planning proposal, change ID, Issue and decisions must be linked")
    check_planning_trace(texts[root + "tasks.md"], {path: texts[path] for path in sorted(specs)})


def protect_planning_baselines(base, head, changed, old_files, files, kind):
    decisions = {path for path in changed if DECISIONS.fullmatch(path)}
    for manifest in sorted(path for path in old_files if PLANNING.fullmatch(path)):
        require(manifest not in changed, "Accepted planning manifest is immutable")
        change_id = PLANNING.fullmatch(manifest)[1]
        root = f"openspec/changes/{change_id}/"
        task_path = root + "tasks.md"
        require(task_path not in changed or kind == "implementation",
                "Only an implementation manifest can update approved planning tasks")
        affected = {path for path in changed if path.startswith(root) and path != task_path}
        archive_roots = {p.split("/", 4)[3] for p in changed & files if re.fullmatch(
            rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(change_id)}/.+", p)}
        archiving = bool(archive_roots) or task_path in old_files - files
        if not (affected or decisions or archiving):
            continue
        record = json.loads(git("show", f"{base}:{manifest}"), object_pairs_hook=unique_object)
        require(record["decisions"] not in decisions, "Approved business decisions are immutable")
        require(affected <= record["artifacts"].keys(), "Cannot add an artifact to an accepted planning baseline")
        if not (affected or archiving):
            continue
        require(kind == "implementation" and not any(p.startswith(root) for p in files),
                "Approved planning artifacts are immutable outside a complete archive")
        frozen = {path for path in record["artifacts"] if path.startswith(root) and path != task_path}
        require(affected == frozen, "Archive must move every approved planning artifact")
        require(len(archive_roots) == 1, "Approved planning artifacts must share one archive directory")
        archive_root = f"openspec/changes/archive/{archive_roots.pop()}/"
        require(archive_root + "tasks.md" in changed & files, "Approved tasks must move to the same archive")
        for path in sorted(affected):
            archived = archive_root + path[len(root):]
            require(archived in changed & files
                    and git_bytes("cat-file", "blob", f"{head}:{archived}")
                    == git_bytes("cat-file", "blob", f"{base}:{path}"),
                    f"Approved planning artifact can only be archived unchanged: {path}")


def accepted_slice_trace(read, base, change_id, issue, old_files, task_path, selected_tasks):
    manifest = f"openspec/verification/{change_id}/planning.json"
    base_tasks = f"openspec/changes/{change_id}/tasks.md"
    if manifest not in old_files:
        return None
    require(base_tasks in old_files, "Archived planning baseline has no remaining implementation slices")
    approval = json.loads(read(manifest, base), object_pairs_hook=unique_object)
    require(approval["issue"] == issue, "Implementation Issue must match the approved planning Issue")
    specs = {path: read(path, base) for path in approval["artifacts"] if SPEC.fullmatch(path)}
    approved = check_planning_trace(read(base_tasks, base), specs, allow_completed=True)
    current = check_planning_trace(read(task_path), specs, allow_completed=True)
    require([entry[0] for entry in current.values()] == [entry[0] for entry in approved.values()],
            "Implementation must preserve the approved planning task rows")
    for task, (_, done, _) in current.items():
        previous = approved[task][1]
        require(not previous or done, "Implementation cannot reopen an accepted task")
        require(done == previous or task in selected_tasks,
                f"Newly completed planning task is outside the declared slice: {task}")
    return {task: approved[task][2] for task in selected_tasks}


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
    protect_planning_baselines(base, head, changed, old_files, files, record["kind"])
    targets = record["targets"]
    links = record["links"]
    require(isinstance(targets, dict) and isinstance(links, list),
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
    if record["kind"] == "planning":
        check_planning(record, manifest_path, change_id, head, changed, old_files, files)
        return change_id, 0, "planning"
    require(not PLANNING.fullmatch(manifest_path), "planning.json is reserved for kind planning")
    require(links, "Explicit verification links are required")
    tasks = unique_strings(record["tasks"], "tasks")
    planning_trace = None
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
        task_text = read(task_paths[0]).lstrip("\ufeff").replace("\r\n", "\n")
        rows = TASK_LINE.findall(task_text)
        declared = [match[1] for _, text in rows if (match := TASK_ID.match(text))]
        require(len(rows) == len(declared) == len(set(declared)) and tasks <= set(declared),
                "Slice tasks must refer to unique task IDs in tasks.md")
        planning_trace = accepted_slice_trace(read, base, change_id, issue, old_files, task_paths[0], tasks)
        if archive or any(spec.startswith("openspec/specs/") for spec in spec_paths):
            require(all(mark in {"x", "X"} for mark, _ in rows), "Cannot sync or archive with incomplete tasks")
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
        if planning_trace is not None:
            definition = targets[target]
            spec, requirement, scenario = definition.get("spec"), definition["requirement"], definition.get("scenario")
            planned = planning_trace[task]
            synced = {
                ("openspec/specs/" + path.removeprefix(f"openspec/changes/{change_id}/specs/"), ac, sc)
                for path, ac, sc in planned
            } if spec in spec_paths and spec.startswith("openspec/specs/") else set()
            require((spec, requirement, scenario) in planned | synced,
                    f"Verification link is outside approved planning trace: {task}/{target}")
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
    if kind == "planning":
        print(f"SDD planning package OK: {change}. No implementation coverage claimed.")
    else:
        print(f"SDD declarations OK: {change} ({kind}, {links} links). Test execution is separate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
