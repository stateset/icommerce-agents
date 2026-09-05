"""Read-only release preflight; missing evidence or GitHub access never means ready.

This prepares a release review, not publication. The protected release workflow
still reruns verification and requires a human to review external evidence.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_check import REQUIRED_GATES, validate_evidence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CHECKS = {"python (3.12)", "web", "analyze (python)", "analyze (javascript-typescript)"}


def command(*args: str) -> str:
    return subprocess.check_output(
        args, cwd=ROOT, text=True, stderr=subprocess.PIPE, timeout=30
    ).strip()


def github_checks(repo: str, commit: str) -> list[dict]:
    """Inspect exact-commit checks, release reviewers, and scanner settings."""
    checks = []

    def inspect(name, endpoint, predicate):
        try:
            data = json.loads(command("gh", "api", f"repos/{repo}/{endpoint}".rstrip("/")))
            passed = predicate(data)
            checks.append({"name": name, "status": "pass" if passed else "fail"})
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            checks.append(
                {
                    "name": name,
                    "status": "unverified",
                    "detail": "GitHub data unavailable or malformed",
                }
            )

    def required_ci(data):
        if data["total_count"] > 100:
            return False
        latest = {}
        for run in sorted(data["check_runs"], key=lambda item: item["id"]):
            if run.get("app", {}).get("slug") == "github-actions":
                latest[run["name"]] = run
        return all(
            name in latest
            and latest[name]["head_sha"] == commit
            and latest[name]["status"] == "completed"
            and latest[name]["conclusion"] == "success"
            for name in REQUIRED_CHECKS
        )

    inspect("candidate_ci", f"commits/{commit}/check-runs?per_page=100", required_ci)
    for environment in ("production-release", "live-evals"):
        inspect(
            environment,
            f"environments/{environment}",
            lambda data: any(
                rule.get("type") == "required_reviewers" and rule.get("reviewers")
                for rule in data.get("protection_rules", [])
            ),
        )
    inspect(
        "secret_protection",
        "",
        lambda data: all(
            data.get("security_and_analysis", {}).get(name, {}).get("status") == "enabled"
            for name in ("secret_scanning", "secret_scanning_push_protection")
        ),
    )
    inspect(
        "branch_protection",
        "branches/master/protection",
        lambda data: (
            REQUIRED_CHECKS <= set(data.get("required_status_checks", {}).get("contexts", []))
            and data.get("required_pull_request_reviews", {}).get(
                "required_approving_review_count", 0
            )
            >= 1
        ),
    )
    return checks


def assess(target_version: str, evidence_path: Path | None, repo: str | None) -> dict:
    checks = []
    commit = command("git", "rev-parse", "HEAD")
    dirty = bool(command("git", "status", "--porcelain"))
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    checks.append({"name": "clean_candidate", "status": "fail" if dirty else "pass"})
    checks.append(
        {
            "name": "package_version",
            "status": "pass" if version == target_version else "fail",
            "actual": version,
        }
    )
    problems = []
    try:
        if evidence_path is None:
            raise ValueError("no evidence document supplied")
        document = json.loads(evidence_path.read_text())
        if not isinstance(document, dict):
            raise ValueError("evidence document must be an object")
        problems = validate_evidence(document, target_version=target_version, commit_sha=commit)
    except (OSError, ValueError) as error:
        problems = [str(error), "Required gates: " + ", ".join(REQUIRED_GATES)]
    checks.append(
        {
            "name": "deployment_evidence",
            "status": "fail" if problems else "pass",
            "problems": problems,
        }
    )
    checks.extend(
        github_checks(repo, commit)
        if repo
        else [
            {
                "name": "github_controls",
                "status": "unverified",
                "detail": "supply --repo to inspect GitHub",
            }
        ]
    )
    return {
        "commit_sha": commit,
        "target_version": target_version,
        "ready_for_release_review": all(check["status"] == "pass" for check in checks),
        "checks": checks,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-version", default="1.0.0")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--repo", help="GitHub owner/repository (read-only)")
    args = parser.parse_args(argv)
    if args.repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/repository")
    report = assess(args.target_version, args.evidence, args.repo)
    print(json.dumps(report, indent=2))
    return 0 if report["ready_for_release_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
