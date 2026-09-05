# Releasing

## Pre-`1.0` releases

Run the deterministic verification commands in the README, update `CHANGELOG.md` and
both version references (`pyproject.toml`, README badge), commit, create an annotated
tag, and push the commit and tag atomically. Do not move or reuse a published tag.

## Production/GA releases

`v1.0.0` and later must use `.github/workflows/release.yml`. The workflow checks out an
existing immutable tag, verifies the package version, reruns the complete deterministic
suite and both web builds, validates external evidence against the exact commit, creates
an SPDX 2.3 SBOM and source bundle, attests their build provenance through GitHub OIDC,
and publishes a GitHub release with SHA-256 checksums.

The release also runs `python scripts/browser_check.py` against both built apps,
an isolated keyless host, and real Chromium. Run this locally with Node 22 on PATH;
it fails on missing prerequisites, uses temporary data and loopback ports, and
stops its child processes on exit.

Configure a protected GitHub environment named `production-release` with required
reviewers and a secret named `PRODUCTION_RELEASE_EVIDENCE`. Set the secret to the JSON
document described in `release-evidence/README.md` only after the candidate commit is
known. Evidence expires after 30 days and must cover live Claude behavior, real OIDC,
testnet stablecoin checkout and refund, ambiguous settlement recovery, encrypted restore,
failure/load behavior, and deployment-owned tax/fulfillment/returns acceptance.

Produce the live-model portion with `python -m evals.run --require-key --repetitions 3
--report live-evals.json`. The structured artifact records model IDs, commit,
working-tree cleanliness, and each verdict. A failed or interrupted run cannot
produce a passing report. Run against a clean candidate checkout; results from
uncommitted changes do not establish evidence for that commit.

Before the first GA release, also require the `python (3.12)`, `web`, and both CodeQL
checks on `master`; enable private vulnerability reporting and secret scanning; and
review every open high/critical dependency or code-scanning alert. These are GitHub
repository settings and cannot be enforced merely by committing a workflow file.

The tag is created before dispatch so the evidence can bind to its exact commit. If the
gate fails, delete only the unpublished candidate tag as appropriate, correct the code
or evidence, and create a new candidate. Never replace a tag after a GitHub release has
been published.

## Stored-data upgrades and rollback

The adapter records applied versions in `icommerce_agent_schema_migrations` and refuses
to open a database created by newer application code. Every schema change must be one
forward-only, transactional migration with an upgrade test starting from the prior
release schema. Before deploying, create and verify an online backup.

Application rollback is allowed only when the target version supports the installed
schema. Otherwise stop traffic, restore the pre-deploy backup into a new file, verify
`PRAGMA integrity_check`, start the old version against that restored file, and switch
traffic only after `/readyz` and order/payment/reconciliation checks pass. Never attempt
an ad-hoc destructive down migration on the live database.
