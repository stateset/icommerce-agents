from datetime import UTC, datetime, timedelta

import pytest

from scripts.live_eval_check import report_digest
from scripts.release_check import REQUIRED_GATES, validate_evidence

SHA = "a" * 40
NOW = datetime(2026, 9, 4, 18, 0, tzinfo=UTC)


def evidence(report) -> dict:
    return {
        "format_version": 1,
        "target_version": "1.0.0",
        "commit_sha": SHA,
        "gates": {
            gate: {
                "passed": True,
                "tested_at": "2026-09-04T17:00:00Z",
                "evidence_url": f"https://evidence.example/{gate}",
                "evidence_sha256": "sha256:" + "c" * 64,
                "summary": f"Production rehearsal passed for {gate}.",
                **(
                    {
                        "passed_cases": 36,
                        "total_cases": 36,
                        "report": report,
                        "evidence_sha256": report_digest(report),
                    }
                    if gate == "live_claude_evals"
                    else {}
                ),
            }
            for gate in REQUIRED_GATES
        },
    }


def test_complete_current_evidence_passes(live_report):
    assert (
        validate_evidence(evidence(live_report), target_version="1.0.0", commit_sha=SHA, now=NOW)
        == []
    )


def test_missing_failed_and_wrong_commit_evidence_fails_together(live_report):
    document = evidence(live_report)
    document["commit_sha"] = "b" * 40
    document["gates"].pop("stablecoin_refund")
    document["gates"]["live_claude_evals"]["passed"] = False

    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)

    assert any("commit_sha" in problem for problem in problems)
    assert any("missing gate 'stablecoin_refund'" in problem for problem in problems)
    assert any("live_claude_evals" in problem and "not passed" in problem for problem in problems)


def test_stale_or_unverifiable_evidence_fails(live_report):
    document = evidence(live_report)
    document["gates"]["backup_restore"]["tested_at"] = (NOW - timedelta(days=31)).isoformat()
    document["gates"]["failure_and_load"]["evidence_url"] = "file:///tmp/result.txt"
    document["gates"]["stablecoin_checkout"]["evidence_sha256"] = "sha256:nope"

    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)

    assert any(
        "backup_restore" in problem and "older than 30 days" in problem for problem in problems
    )
    assert any("failure_and_load" in problem and "HTTPS" in problem for problem in problems)
    assert any(
        "stablecoin_checkout" in problem and "evidence_sha256" in problem for problem in problems
    )


@pytest.mark.parametrize("passed_cases", [17, 18, 35, 37, True, 36.0])
def test_live_eval_evidence_requires_all_three_runs_to_pass(passed_cases, live_report):
    document = evidence(live_report)
    document["gates"]["live_claude_evals"]["passed_cases"] = passed_cases

    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)

    assert any("36/36" in problem for problem in problems)


def test_baseline_only_evidence_cannot_satisfy_expanded_suite(live_report):
    document = evidence(live_report)
    document["gates"]["live_claude_evals"].update(passed_cases=18, total_cases=18)
    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)
    assert any("36/36" in problem for problem in problems)


def test_claimed_total_without_report_is_not_release_evidence(live_report):
    document = evidence(live_report)
    del document["gates"]["live_claude_evals"]["report"]
    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)
    assert "live report must be an object" in problems


def test_embedded_report_must_match_linked_artifact_digest(live_report):
    document = evidence(live_report)
    live_report["models"]["shopping"] = "different-model"
    problems = validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)
    assert "live report checksum does not match evidence_sha256" in problems


def test_rehashed_failed_report_cannot_pass(live_report):
    live_report["runs"][1]["results"][0]["passed"] = False
    problems = validate_evidence(
        evidence(live_report), target_version="1.0.0", commit_sha=SHA, now=NOW
    )
    assert any("non-passing result" in problem for problem in problems)


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://user:secret@example.com/run",
        "https://replace.invalid/run",
        "https://example.com:invalid/run",
        "https://exa mple.com/run",
    ],
)
def test_malformed_or_credential_bearing_evidence_urls_are_rejected(url, live_report):
    document = evidence(live_report)
    document["gates"]["backup_restore"]["evidence_url"] = url
    assert any(
        "evidence_url" in problem
        for problem in validate_evidence(document, target_version="1.0.0", commit_sha=SHA, now=NOW)
    )
