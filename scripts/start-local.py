"""Start the local Compose stack with bounded startup and a migration interlock."""

import argparse
import subprocess
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project")
    parser.add_argument("--env-file")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update after stopping API and taking a backup",
    )
    parser.add_argument("--startup-timeout", type=float, default=90)
    args = parser.parse_args()
    if not 0 < args.startup_timeout <= 90:
        parser.error("--startup-timeout must be in (0, 90]")
    compose = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
    if args.project:
        compose.extend(("--project-name", args.project))
    if args.env_file:
        compose.extend(("--env-file", args.env_file))
    deadline = monotonic() + args.startup_timeout

    def run(command: list[str], budget: float, *, capture: bool = False) -> str:
        remaining = min(budget, deadline - monotonic())
        if remaining <= 0:
            raise subprocess.TimeoutExpired(compose, budget)
        result = subprocess.run(
            [*compose, *command],
            cwd=ROOT,
            check=True,
            text=True,
            timeout=remaining,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.DEVNULL if capture else None,
        )
        return result.stdout.strip() if capture else ""

    stage = "API preflight"
    try:
        active = ("running", "restarting", "paused", "removing", "dead")
        status_filters = [f"--status={status}" for status in active]
        if run(["ps", "--all", *status_filters, "-q", "api"], 5, capture=True):
            print(
                "API is still running. Stop it and verify the backup before migration."
            )
            return 1
        if args.update and not run(["ps", "-aq", "db"], 5, capture=True):
            print("Update requires an existing database container.")
            return 1
        stage = "PostgreSQL startup"
        run(["up", "-d", "--no-build", "--wait", "--wait-timeout", "39", "db"], 40)
        stage = "migration"
        run(
            [
                "up",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "--exit-code-from",
                "migrate",
                "migrate",
            ],
            31,
        )
        stage = "API startup"
        run(
            [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "14",
                "api",
            ],
            15,
        )
    except (OSError, subprocess.SubprocessError):
        print(f"Startup failed during {stage}. Check Compose service state.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
