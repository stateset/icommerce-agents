import json
import subprocess

from scripts import release_readiness as readiness

SHA = "a" * 40


def test_dirty_candidate_and_missing_evidence_cannot_be_ready(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.9.0"\n')
    monkeypatch.setattr(readiness, "ROOT", tmp_path)
    monkeypatch.setattr(
        readiness, "command", lambda *args: SHA if args[1] == "rev-parse" else " M changed.py"
    )
    report = readiness.assess("1.0.0", None, None)
    assert report["ready_for_release_review"] is False
    assert {check["name"]: check["status"] for check in report["checks"]} == {
        "clean_candidate": "fail",
        "package_version": "fail",
        "deployment_evidence": "fail",
        "github_controls": "unverified",
    }


def test_unavailable_github_controls_are_unverified(monkeypatch):
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(readiness, "command", unavailable)
    checks = readiness.github_checks("owner/repo", SHA)
    assert len(checks) == 5
    assert all(check["status"] == "unverified" for check in checks)


def test_latest_ci_failure_overrides_an_older_success(monkeypatch):
    runs = [
        {
            "id": index,
            "name": name,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "app": {"slug": "github-actions"},
        }
        for index, name in enumerate(sorted(readiness.REQUIRED_CHECKS))
    ]
    runs.append({**runs[0], "id": 100, "conclusion": "failure"})

    def api(*args):
        if "check-runs" in args[-1]:
            return json.dumps({"total_count": len(runs), "check_runs": runs})
        return "{}"

    monkeypatch.setattr(readiness, "command", api)
    assert readiness.github_checks("owner/repo", SHA)[0]["status"] == "fail"
    runs.pop()
    assert readiness.github_checks("owner/repo", SHA)[0]["status"] == "pass"
    runs[0]["head_sha"] = "b" * 40
    assert readiness.github_checks("owner/repo", SHA)[0]["status"] == "fail"
