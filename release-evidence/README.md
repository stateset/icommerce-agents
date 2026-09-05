# Production release evidence

GA releases require a JSON evidence document reviewed through the protected
`production-release` GitHub environment. The document is bound to the exact release
commit and expires after 30 days. Run:

```bash
python scripts/release_check.py \
  --evidence release-evidence/v1.0.0.json \
  --target-version 1.0.0 \
  --commit "$(git rev-parse HEAD)"
```

The document has `format_version`, `target_version`, `commit_sha`, and one object under
`gates` for every key in `scripts.release_check.REQUIRED_GATES`. Each gate contains:

```json
{
  "passed": true,
  "tested_at": "2026-09-04T17:00:00Z",
  "evidence_url": "https://durable.example/evidence/run-123",
  "evidence_sha256": "sha256:replace-with-the-linked-artifact-digest",
  "summary": "What was tested, against which real systems, and the observed result."
}
```

The `live_claude_evals` object additionally requires `passed_cases: 36` and
`total_cases: 36`, representing twelve cases across three consecutive runs.
The suite includes six baseline cases and six explicit user-pressure variants.
Older 18-result evidence does not satisfy the expanded gate.

This gate must also contain `report`: the complete parsed JSON object from the
live-eval runner, not just a passing total or a link. Its `evidence_sha256` must
match the SHA-256 of that report's canonical artifact bytes (sorted JSON keys,
two-space indentation, UTF-8, one trailing newline), which the runner emits.
The linked artifact must be that report. Checks reject missing/duplicate cases,
incomplete or failed runs, mismatched commits, dirty worktrees, missing model IDs,
invalid timestamps, and checksum mismatches. Counts-only evidence is no longer
accepted. The template deliberately leaves `report` null until a real run exists.

Generate the underlying model report with:

```bash
python -m evals.run --require-key --repetitions 3 --report /tmp/live-evals.json
python scripts/live_eval_check.py --report /tmp/live-evals.json \
  --commit "$(git rev-parse HEAD)"
```

Review all three runs, the model IDs, the commit SHA, and `worktree_dirty: false`
before marking the live-model gate passed. The runner fails on unknown case IDs
and missing credentials in report mode, retains failed verdicts, and writes a
non-passing partial report if a provider call fails. Its report is supporting
evidence, not a replacement for the full eight-gate release document.
Keep reports and redirected logs outside the candidate checkout so artifact creation
does not make an otherwise clean source tree appear dirty. The GitHub workflow uses
the runner's temporary directory for this reason.

Do not commit credentials, payment payloads, customer data, wallet addresses, or raw
tokens. Link to access-controlled evidence instead. A reviewer must confirm the links
before approving the release environment; this schema proves completeness and recency,
not the truth of an arbitrary URL.
Neither the embedded report nor its checksum proves that provider calls occurred:
reviewers must inspect the originating protected workflow and confirm the reported
model IDs are appropriate for the release. Do not hand-author a passing report.
