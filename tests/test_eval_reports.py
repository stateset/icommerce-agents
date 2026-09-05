import hashlib
import json

import pytest

from evals import run as runner
from evals.graders import EvalResult
from scripts.live_eval_check import report_digest


def test_unknown_case_is_rejected_instead_of_reporting_zero_passes():
    with pytest.raises(SystemExit) as error:
        runner.main(["typo-in-case-name"])
    assert error.value.code == 2


def test_required_key_cannot_silently_skip(monkeypatch):
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", lambda: None)
    assert runner.main(["--require-key"]) == 2


def test_report_request_cannot_silently_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", lambda: None)
    assert runner.main(["--report", str(tmp_path / "report.json")]) == 2
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("failed_run", [None, 2])
def test_report_records_all_repetitions_and_failure(tmp_path, monkeypatch, failed_run):
    class Client:
        closed = False

        async def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", lambda: client)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kwargs: "a" * 40 if command[1] == "rev-parse" else " M changed.py",
    )
    repetitions = 0

    async def iter_results(cases, current_client, **kwargs):
        nonlocal repetitions
        assert current_client is client
        repetitions += 1
        for case in cases:
            yield EvalResult(case.id, repetitions != failed_run, "checked")

    monkeypatch.setattr(runner, "_iter_results", iter_results)
    report_path = tmp_path / "report.json"
    assert runner.main(["--repetitions", "3", "--report", str(report_path)]) == (
        0 if failed_run is None else 1
    )
    report = json.loads(report_path.read_text())
    assert report_digest(report) == "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert report["commit_sha"] == "a" * 40
    assert report["worktree_dirty"] is True
    assert len(report["runs"]) == 3
    assert sum(len(run["results"]) for run in report["runs"]) == 36
    assert report["passed"] is (failed_run is None)
    assert client.closed


def test_interrupted_eval_writes_failed_report_and_closes_client(tmp_path, monkeypatch):
    class Client:
        closed = False

        async def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", lambda: client)
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *args, **kwargs: "a" * 40)

    async def fail(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(runner, "_run_case", fail)
    report_path = tmp_path / "report.json"
    with pytest.raises(RuntimeError, match="provider unavailable"):
        runner.main(["--repetitions", "3", "--report", str(report_path)])
    report = json.loads(report_path.read_text())
    assert report["passed"] is False
    assert report["runs"] == [{"repetition": 1, "results": []}]
    assert report["failure_type"] == "RuntimeError"
    assert client.closed
