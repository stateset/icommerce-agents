# GA readiness

The package remains pre-1.0 until the release process accepts evidence for the
exact candidate commit. A passing deterministic suite is necessary but does not
prove live-model or provider behavior.

Run the read-only preflight to list current blockers as JSON:

```bash
python scripts/release_readiness.py --repo stateset/icommerce-agents \
  --target-version 1.0.0 --evidence release-evidence/v1.0.0.json
```

It checks candidate cleanliness/version, the eight deployment evidence gates,
exact-commit CI results, protected environment reviewers, branch protection, and
secret-scanning controls. Missing GitHub access is `unverified`, never a pass.
Exit code 0 means ready for release review; publishing still requires the protected
release workflow and human review of the linked evidence. Repositories using
rulesets instead of classic branch protection need a separate ruleset review;
this command conservatively leaves that branch-protection check unverified.

## Implementation and verification added on 2026-09-05

- Cancellation-safe engine execution and chat persistence.
- OS-held turn locks, tested using real worker pause, expired lease, process kill,
  and recovery. See the mandatory first-upgrade drain procedure in `operations.md`.
- Immutable customer/operator, role, store, and authenticated-subject bindings.
- Three-run structured Claude eval reports with model IDs, commit identity,
  working-tree status, and failed/partial results retained.
- Mandatory Chromium verification in the production release workflow, using
  `scripts/browser_check.py` to manage an isolated host and both built apps.
- Stricter release-evidence URL and numeric-field validation.
- UTC-normalized, immutable principal bindings, with legacy-offset cleanup,
  sub-millisecond expiry boundaries, and concurrent renewal regression tests.
- Twelve live scenarios (six baseline plus six user-pressure variants), with
  graders rejecting empty/error turns and price claims preceding successful retrieval.

An earlier local full deterministic run passed **365 tests with no skips**, using
Node 22.18.0, with one upstream Starlette/AnyIO deprecation warning. Repository-wide
lint, formatting, and drift checks also passed. This is evidence for the working
tree at that time, not a reviewed release commit or a new live-model result. That
full run predates the latest eval-report recovery, setup-integrity, and environment
preflight changes; it must not be presented as verification of the current tree.

The latest environment/setup checks passed twice (21 tests, then 22 after adding
startup-hook coverage), including the real-cart regression. Repository-wide lint,
formatting (105 files), and whitespace checks passed. A new full-suite attempt
outside the restrictive sandbox exceeded its 600-second process deadline without
a final result; the separate drift check exceeded 30 seconds. The host had nearly
exhausted RAM and fully occupied swap during these attempts. Neither interrupted
check counts as a pass, and a fresh complete run remains required. The diagnosed
socket-notification failure and its regression coverage are recorded in
`integration-review.md`.

Both applications were built under Node 22.18.0 and passed the real browser smoke
test. `npm audit --audit-level=high` reported zero vulnerabilities; `uv pip check`
reported compatible installed dependencies. These are local observations, not
evidence for a future immutable release commit.

## External setup and evidence still required

Repository inspection on 2026-09-05 found no branch protection or rulesets and no
release environments. Secret scanning and push protection were subsequently
enabled, along with private vulnerability reporting; the checked open Dependabot
and secret-scanning alert lists were empty.

1. Choose required reviewers for `production-release` and `live-evals`, create
   those protected environments, and configure credentials through GitHub secrets.
   Never put API keys, signing material, or raw identity tokens in an evidence file.
2. Commit and review the candidate, run its CI including CodeQL, then require
   `python (3.12)`, `web`, and both CodeQL jobs on `master`. The new workflows must
   exist on GitHub before their checks can provide candidate evidence.
3. On a clean candidate checkout, run all twelve Claude cases three times using
   `--require-key --repetitions 3 --report`. The historical 4/6 result remains the
   last documented live result until a real run supersedes it.
   The expanded suite adds six user-pressure variants; its release gate is 36/36,
   not the earlier baseline-only 18/18. No live result exists for those new cases yet.
   Embed the complete report in the live-model gate, with its canonical artifact
   SHA-256. `scripts/live_eval_check.py` verifies per-run case coverage and provenance
   fields; a passing total without its matching report is rejected.
4. Exercise the actual OIDC issuer, testnet facilitator/wallet, and treasury refund
   adapter. Rehearse ambiguous settlement without duplicate charging.
5. Rehearse encrypted backup/restore and worker failure under the deployment's
   expected peak workload. Record workload, latency/error objectives, observed
   outcomes, and recovery times; unit tests are not deployment load evidence.
6. Obtain acceptance for deployment-owned tax, fulfillment, and returns integrations.
7. Assemble the eight-gate evidence document, binding every artifact to the exact
   candidate commit with fresh timestamps and SHA-256 digests. Update the package
   version and release badge as part of the final reviewed candidate, then use the
   protected release workflow. Do not hand-edit gates to stand in for rehearsals.

`release-evidence/README.md` defines the evidence format and `docs/releasing.md`
defines publication. These local results alone do not establish stable 1.0 readiness.
