"""Validate deployment evidence before publishing a production/GA release.

The evidence is deliberately external to deterministic CI: it records rehearsals that
need real identity, model, payment, wallet, recovery, and infrastructure systems.  A
protected GitHub environment remains responsible for reviewing whether linked evidence
is genuine; this script makes omissions, stale runs, and commit drift machine-failing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.live_eval_check import (  # noqa: E402
    LIVE_EVAL_RESULTS,
    report_digest,
    validate_live_report,
)

EVIDENCE_FORMAT = 1
MAX_EVIDENCE_AGE = timedelta(days=30)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
REQUIRED_GATES = {
    "live_claude_evals": "all twelve cases passed in three consecutive runs",
    "oidc_authorization": "real issuer, audience, roles, scopes, and tenant isolation",
    "stablecoin_checkout": "wallet and facilitator completed a testnet checkout",
    "stablecoin_refund": "treasury/provider returned funds on-chain and accounting reconciled",
    "ambiguous_settlement_recovery": "crash/timeout recovery was rehearsed without a double charge",
    "backup_restore": "an encrypted production-shaped backup restored and passed integrity checks",
    "failure_and_load": "worker loss and expected peak load stayed inside the support envelope",
    "tax_fulfillment_returns": "deployment integrations and operational ownership were accepted",
}


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def validate_evidence(
    document: dict[str, Any], *, target_version: str, commit_sha: str, now: datetime | None = None
) -> list[str]:
    """Return every release-gate failure rather than stopping at the first one."""
    problems: list[str] = []
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    if (
        type(document.get("format_version")) is not int
        or document["format_version"] != EVIDENCE_FORMAT
    ):
        problems.append(f"format_version must be {EVIDENCE_FORMAT}")
    if document.get("target_version") != target_version:
        problems.append(f"target_version must be {target_version!r}")
    if not VERSION_RE.fullmatch(target_version):
        problems.append("target_version must be a stable semantic version")
    if not SHA_RE.fullmatch(commit_sha):
        problems.append("expected commit must be a full lowercase 40-character SHA")
    if document.get("commit_sha") != commit_sha:
        problems.append("evidence commit_sha does not match the release commit")

    gates = document.get("gates")
    if not isinstance(gates, dict):
        return [*problems, "gates must be an object"]
    for gate, expectation in REQUIRED_GATES.items():
        item = gates.get(gate)
        if not isinstance(item, dict):
            problems.append(f"missing gate {gate!r}: {expectation}")
            continue
        if item.get("passed") is not True:
            problems.append(f"gate {gate!r} has not passed: {expectation}")
        summary = item.get("summary")
        if not isinstance(summary, str) or len(summary.strip()) < 12:
            problems.append(f"gate {gate!r} needs a meaningful summary")
        url = item.get("evidence_url")
        try:
            parsed_url = urlsplit(url) if isinstance(url, str) else None
            valid_url = (
                parsed_url is not None
                and parsed_url.scheme == "https"
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not any(character.isspace() for character in str(url))
                and not (parsed_url.hostname or "").endswith(".invalid")
            )
            if parsed_url is not None:
                _ = parsed_url.port  # Validate malformed ports as well as host syntax.
        except ValueError:
            valid_url = False
        if not valid_url:
            problems.append(f"gate {gate!r} evidence_url must be a valid credential-free HTTPS URL")
        evidence_sha256 = item.get("evidence_sha256")
        if not isinstance(evidence_sha256, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", evidence_sha256
        ):
            problems.append(f"gate {gate!r} evidence_sha256 must bind the linked artifact")
        try:
            tested_at = _timestamp(item.get("tested_at"), f"gates.{gate}.tested_at")
        except ValueError as error:
            problems.append(str(error))
        else:
            if tested_at > observed_now + timedelta(minutes=5):
                problems.append(f"gate {gate!r} tested_at is in the future")
            elif observed_now - tested_at > MAX_EVIDENCE_AGE:
                problems.append(f"gate {gate!r} evidence is older than 30 days")
        if gate == "live_claude_evals":
            report = item.get("report")
            problems.extend(validate_live_report(report, commit_sha=commit_sha, now=observed_now))
            if isinstance(report, dict):
                try:
                    if evidence_sha256 != report_digest(report):
                        problems.append("live report checksum does not match evidence_sha256")
                except (TypeError, ValueError):
                    problems.append("live report cannot be encoded as canonical JSON")
            if any(
                type(item.get(key)) is not int or item[key] != LIVE_EVAL_RESULTS
                for key in ("passed_cases", "total_cases")
            ):
                problems.append(
                    "gate 'live_claude_evals' must record exactly "
                    f"{LIVE_EVAL_RESULTS}/{LIVE_EVAL_RESULTS} passing cases"
                )
    unknown = sorted(set(gates) - set(REQUIRED_GATES))
    if unknown:
        problems.append(f"unknown gates: {', '.join(unknown)}")
    return problems


def _head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--commit", default=None)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.evidence.read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f"release evidence cannot be read: {error}", file=sys.stderr)
        return 2
    if not isinstance(document, dict):
        print("release evidence must be a JSON object", file=sys.stderr)
        return 2
    problems = validate_evidence(
        document,
        target_version=args.target_version,
        commit_sha=args.commit or _head_sha(),
    )
    if problems:
        print("production release gate failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"production release evidence accepted for {args.target_version} "
        f"at {document['commit_sha']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
