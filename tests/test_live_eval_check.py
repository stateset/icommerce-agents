import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.live_eval_check import report_digest, serialize_report, validate_live_report

NOW = datetime(2026, 9, 4, 18, tzinfo=UTC)
SHA = "a" * 40


def test_complete_candidate_report_passes(live_report):
    assert validate_live_report(live_report, commit_sha=SHA, now=NOW) == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("format_version",), True),
        (("format_version",), 1.0),
        (("commit_sha",), "b" * 40),
        (("worktree_dirty",), True),
        (("worktree_dirty",), 0),
        (("passed",), 1),
        (("failure_type",), "TimeoutError"),
        (("case_ids",), []),
        (("requested_repetitions",), 3.0),
        (("models",), []),
        (("models", "shopping"), " "),
        (("models", "merchant"), None),
        (("started_at",), "2026-09-04T16:00:00"),
        (("started_at",), "2026-01-01T00:00:00Z"),
        (("started_at",), "2026-09-04T17:01:00Z"),
        (("finished_at",), "2026-09-04T19:00:00Z"),
        (("finished_at",), None),
        (("runs",), []),
        (("runs", 0), None),
        (("runs", 0, "repetition"), True),
        (("runs", 1, "repetition"), 1),
        (("runs", 2, "results"), []),
        (("runs", 0, "results", 0), None),
        (("runs", 1, "results", 0, "case_id"), "unknown-case"),
        (("runs", 2, "results", 0, "passed"), False),
        (("runs", 2, "results", 0, "passed"), "true"),
        (("runs", 2, "results", 0, "reason"), ""),
    ],
)
def test_malformed_or_incomplete_report_fails(live_report, path, value):
    target = live_report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert validate_live_report(live_report, commit_sha=SHA, now=NOW)


def test_thirty_six_results_cannot_hide_duplicate_cases(live_report):
    for run in live_report["runs"]:
        run["results"] = [run["results"][0]] * 12
    assert validate_live_report(live_report, commit_sha=SHA, now=NOW)


def test_report_digest_binds_canonical_artifact_bytes(live_report):
    encoded = serialize_report(live_report).encode("utf-8")
    assert report_digest(live_report) == "sha256:" + hashlib.sha256(encoded).hexdigest()
    assert serialize_report(dict(reversed(list(live_report.items())))) == encoded.decode()
    assert json.loads(encoded) == live_report


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_json_values_cannot_enter_release_evidence(live_report, value):
    live_report["unexpected"] = value
    assert validate_live_report(live_report, commit_sha=SHA, now=NOW)


@pytest.mark.parametrize("document", [None, [], True, "passed"])
def test_nonobject_report_is_rejected(document):
    assert validate_live_report(document, commit_sha=SHA, now=NOW)


def test_cli_runs_without_installed_sdk_dependencies(tmp_path, live_report):
    now = datetime.now(UTC)
    live_report["started_at"] = (now - timedelta(minutes=2)).isoformat()
    live_report["finished_at"] = (now - timedelta(minutes=1)).isoformat()
    path = tmp_path / "report.json"
    path.write_text(serialize_report(live_report), encoding="utf-8")
    checker = Path(__file__).resolve().parents[1] / "scripts/live_eval_check.py"
    command = [sys.executable, "-S", str(checker), "--report", str(path), "--commit", SHA]
    passed = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert passed.returncode == 0, passed.stderr
    assert "36/36" in passed.stdout
    live_report["runs"].pop()
    path.write_text(serialize_report(live_report), encoding="utf-8")
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 1
    path.write_text("not JSON", encoding="utf-8")
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 2
