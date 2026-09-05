import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from evals import run as runner
from evals.graders import EvalResult
from scripts.live_eval_check import serialize_report, validate_live_report

SHA = "a" * 40


@pytest.fixture
def client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", lambda: client)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kwargs: SHA if command[1] == "rev-parse" else "",
    )
    return client


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
def test_each_completed_case_is_checkpointed_before_the_next_call(
    tmp_path, monkeypatch, client, failure_type
):
    path = tmp_path / "report.json"
    calls = 0

    async def run_case(case, current_client, db_path):
        nonlocal calls
        assert current_client is client
        checkpoint = json.loads(path.read_text())
        assert checkpoint["passed"] is False
        saved = [result for run in checkpoint["runs"] for result in run["results"]]
        assert len(saved) == calls
        calls += 1
        if calls == 3:
            raise failure_type("provider failed: sensitive-provider-detail")
        return EvalResult(case.id, True, "checked")

    monkeypatch.setattr(runner, "_run_case", run_case)
    with pytest.raises(failure_type):
        runner.main(["--repetitions", "3", "--report", str(path)])
    report = json.loads(path.read_text())
    assert len(report["runs"][0]["results"]) == 2
    assert report["failure_type"] == failure_type.__name__
    assert "sensitive-provider-detail" not in path.read_text()
    assert validate_live_report(report, commit_sha=SHA)
    client.close.assert_awaited_once()


def test_metadata_failure_still_closes_client(monkeypatch, client):
    def fail(*args, **kwargs):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(runner.subprocess, "check_output", fail)
    with pytest.raises(RuntimeError, match="git unavailable"):
        runner.main([])
    client.close.assert_awaited_once()


def test_cleanup_failure_cannot_leave_a_passing_report(tmp_path, monkeypatch, client):
    client.close.side_effect = RuntimeError("close failed")

    async def run_case(case, *_):
        return EvalResult(case.id, True, "checked")

    monkeypatch.setattr(runner, "_run_case", run_case)
    path = tmp_path / "report.json"
    with pytest.raises(RuntimeError, match="close failed"):
        runner.main(["--repetitions", "3", "--report", str(path)])
    report = json.loads(path.read_text())
    assert len(report["runs"]) == 3
    assert all(result["passed"] for run in report["runs"] for result in run["results"])
    assert report["passed"] is False
    assert report["failure_type"] == "RuntimeError"


def test_checkpoint_failure_prevents_provider_calls(tmp_path, monkeypatch, client):
    run_case = AsyncMock()
    monkeypatch.setattr(runner, "_run_case", run_case)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_checkpoint_report", fail)
    with pytest.raises(OSError, match="disk full"):
        runner.main(["--report", str(tmp_path / "report.json")])
    run_case.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_atomic_checkpoint_failure_preserves_prior_bytes(tmp_path, monkeypatch, failure_point):
    path = tmp_path / "report.json"
    original = {"passed": False, "runs": []}
    runner._checkpoint_report(path, original)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runner.os, failure_point, fail)
    with pytest.raises(OSError, match="disk full"):
        runner._checkpoint_report(path, {"passed": False, "runs": [{"results": []}]})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
    assert before.decode() == serialize_report(original)


async def test_case_timeout_cancels_and_drains_provider_work(monkeypatch):
    drained = asyncio.Event()

    async def stuck(*args):
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    monkeypatch.setattr(runner, "_run_case", stuck)
    with pytest.raises(TimeoutError):
        async for _ in runner._iter_results(runner.CASES[:1], object(), case_timeout_seconds=0.01):
            pytest.fail("timed-out case produced a result")
    assert drained.is_set()


@pytest.mark.parametrize("value", ["0", "-1", "601", "nan"])
def test_invalid_case_timeout_is_rejected_before_client_creation(monkeypatch, value):
    factory = AsyncMock()
    monkeypatch.setattr("host.anthropic_client.build_anthropic_client", factory)
    with pytest.raises(SystemExit) as error:
        runner.main(["--case-timeout-seconds", value])
    assert error.value.code == 2
    factory.assert_not_called()
