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

## What the deterministic suite does and does not prove

The pytest suite, the drift check, the denial walkthrough, the keyless tour, and the
browser check are all deterministic and keyless. A green run proves the agent-layer
gates, the engine's transactional refusals, the durable control plane (approval claims,
target leases, session identity, turn locks, cancellation safety), the stablecoin state
machine against fake providers, and that both web apps render live engine state. It
proves nothing about a live model's tool choices, a real facilitator or treasury
adapter, an OIDC issuer, or the deployment's load profile. Those are the external
evidence gates below, and each must be bound to the exact candidate commit.

Run results belong in `release-evidence/` and the CHANGELOG, not here: this page
describes the process and stays true across candidates.

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
