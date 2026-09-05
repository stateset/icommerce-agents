"""Validate live-model report structure and candidate binding without SDK dependencies.

This verifies consistency, not authenticity: protected reviewers must still verify
the linked workflow and provider evidence. A hand-authored report is not a live run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BASELINE_CASE_IDS = (
    "shopping-figure-from-tool-result",
    "shopping-fenced-review-not-obeyed",
    "shopping-checkout-described-as-staging",
    "shopping-medical-referral-with-product",
    "merchant-write-confirmed-after-success",
    "merchant-campaign-limitation-not-a-zero",
)
EXPECTED_CASE_IDS = BASELINE_CASE_IDS + tuple(f"{name}-pressure" for name in BASELINE_CASE_IDS)
LIVE_EVAL_CASES = len(EXPECTED_CASE_IDS)
LIVE_EVAL_REPETITIONS = 3
LIVE_EVAL_RESULTS = LIVE_EVAL_CASES * LIVE_EVAL_REPETITIONS


def serialize_report(report: dict[str, Any]) -> str:
    """The canonical artifact encoding shared by generation and release review."""
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"


def report_digest(report: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(serialize_report(report).encode("utf-8")).hexdigest()


def validate_live_report(report: Any, *, commit_sha: str, now: datetime | None = None) -> list[str]:
    if not isinstance(report, dict):
        return ["live report must be an object"]
    problems = []
    try:
        serialize_report(report)
    except (TypeError, ValueError):
        problems.append("live report must contain only JSON-encodable values")
    if type(report.get("format_version")) is not int or report["format_version"] != 1:
        problems.append("live report format_version must be integer 1")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha) or report.get("commit_sha") != commit_sha:
        problems.append("live report commit_sha must match the full release commit")
    if report.get("worktree_dirty") is not False:
        problems.append("live report must come from a clean worktree")
    if report.get("passed") is not True:
        problems.append("live report has not passed")
    if "failure_type" in report:
        problems.append("live report records a runner failure")
    if report.get("case_ids") != list(EXPECTED_CASE_IDS):
        problems.append("live report case_ids must contain the complete ordered twelve-case suite")
    if (
        type(report.get("requested_repetitions")) is not int
        or report["requested_repetitions"] != LIVE_EVAL_REPETITIONS
    ):
        problems.append("live report must request exactly three repetitions")
    models = report.get("models")
    if not isinstance(models, dict) or any(
        not isinstance(models.get(role), str) or not models[role].strip()
        for role in ("shopping", "merchant")
    ):
        problems.append("live report must identify both shopping and merchant models")

    timestamps = {}
    for key in ("started_at", "finished_at"):
        try:
            value = report.get(key)
            if not isinstance(value, str):
                raise ValueError
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError
            timestamps[key] = parsed.astimezone(UTC)
        except ValueError:
            problems.append(f"live report {key} must be a timezone-aware timestamp")
    if len(timestamps) == 2:
        observed = now or datetime.now(UTC)
        if timestamps["started_at"] > timestamps["finished_at"]:
            problems.append("live report finished_at precedes started_at")
        if timestamps["started_at"] < observed - timedelta(days=30):
            problems.append("live report is older than 30 days")
        if timestamps["finished_at"] > observed + timedelta(minutes=5):
            problems.append("live report finished_at is in the future")

    runs = report.get("runs")
    if not isinstance(runs, list) or len(runs) != LIVE_EVAL_REPETITIONS:
        return [*problems, "live report must contain exactly three complete runs"]
    for index, run in enumerate(runs, start=1):
        if not isinstance(run, dict):
            problems.append(f"live report run {index} must be an object")
            continue
        if type(run.get("repetition")) is not int or run["repetition"] != index:
            problems.append(f"live report run {index} has an invalid repetition number")
        results = run.get("results")
        if not isinstance(results, list) or len(results) != LIVE_EVAL_CASES:
            problems.append(f"live report run {index} must contain twelve results")
            continue
        for expected, result in zip(EXPECTED_CASE_IDS, results, strict=True):
            if not isinstance(result, dict):
                problems.append(f"live report run {index} result must be an object")
                continue
            if result.get("case_id") != expected:
                problems.append(
                    f"live report run {index} has missing, duplicate, or reordered cases"
                )
            if result.get("passed") is not True:
                problems.append(f"live report run {index} contains a non-passing result")
            if not isinstance(result.get("reason"), str) or not result["reason"].strip():
                problems.append(f"live report run {index} result needs a verdict reason")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("live report cannot be read as JSON", file=sys.stderr)
        return 2
    problems = validate_live_report(report, commit_sha=args.commit)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"live report structure accepted: {LIVE_EVAL_RESULTS}/{LIVE_EVAL_RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
