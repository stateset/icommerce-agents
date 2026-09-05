# CI

`ci.yml` runs on push to `master` and on every pull request. Two independent jobs;
`web` never blocks `python` — neither has a `needs:` on the other.

The workflow has read-only repository permissions, cancels superseded runs on the same
ref, disables persisted checkout credentials, and pins every GitHub-authored action to
an immutable full commit SHA (the adjacent comment records the reviewed release tag).

`live-evals.yml` is intentionally separate from deterministic CI. It runs weekly and
on manual dispatch through the protected `live-evals` GitHub environment, requires an
`ANTHROPIC_API_KEY`, and executes all twelve behavioral cases three times. Its preflight
fails if the secret is absent, so a skipped no-key run cannot look like evidence that a
model passed. Configure `ANTHROPIC_WORKSPACE_ID` in the same environment when the key is
identity-linked. These runs are billable and may vary as hosted model behavior changes.
The workflow validates the complete structured report, then retains its report, log,
and SHA-256 digests for 30 days. Available artifacts from failed runs are also hashed
and retained. The production gate checks every embedded verdict and binds that report
to the linked artifact's digest; reviewers still verify the originating workflow.

`release.yml` is manual and protected by the `production-release` environment. It will
not publish a GitHub release unless the tag matches the package version and fresh,
commit-bound evidence covers every external production rehearsal. It reruns deterministic
verification, produces an SPDX SBOM and source bundle with checksums, and uses GitHub's
OIDC-backed build-provenance attestation before publishing. See `docs/releasing.md`.

`codeql.yml` scans Python and TypeScript/JavaScript on pushes, pull requests, and a
weekly schedule. Dependabot separately watches Python, npm, GitHub Actions, and the
Anthropic git submodule. Branch protection, private vulnerability reporting, secret
scanning, and protected release-environment reviewers are repository settings and must
be enabled by an administrator; workflow files cannot silently grant those controls.

## `python`

Checks out the repo with `submodules: recursive` (`vendor/commerce-agents` is a git
submodule), installs `requirements-dev.txt`, and runs, in order: `pip check`,
`ruff check .`, `ruff format --check .`, `mypy` (over `engine_backend/`, `host/`, and
`mcp_servers/`, configured in `pyproject.toml`), `pytest` (parallel through
`pytest-xdist`; each worker seeds its own engine template), `scripts/check.py` (the drift check between code
and documentation), `scripts/denials.py` (the three end-to-end refusals: a cart
write blocked for lack of provenance, an apply blocked for lack of approval, and an
over-refund the engine itself rejects inside the transaction), and `scripts/tour.py`
run twice against the same db (a placed order through the governed `checkout.commit`,
an unapproved apply refused, and both evidence kinds — `activity_log` and a sealed
`kernel_receipt`). Twice, same db, because re-runnability against existing state was
itself a bug once; this is where a regression in that would show up first.

Matrix: Python 3.12 only. `pyproject.toml` pins `requires-python = ">=3.12,<3.13"`,
so a 3.13 job cannot install the package; the matrix stays single-version until that
cap is deliberately widened.

`stateset-embedded==1.28.5` publishes `manylinux_2_34` wheels, and `ubuntu-latest`'s
glibc satisfies that, so installing normally takes well under a minute. A runner with
an older glibc falls back to building the engine from its cargo/maturin sdist —
compiling Rust from source, which can take 15+ minutes. The job's `timeout-minutes: 30`
accounts for that fallback.

Pip is cached via `actions/setup-python`'s `cache: pip`.

## `web`

Checks out the repo (submodules too, since the web workspace depends on
`vendor/commerce-agents/examples/web-shared`), installs Node via `actions/setup-node`
with `cache: npm`, runs `npm ci`, then `npm audit --audit-level=high`, then
`npm run typecheck` (`tsc --noEmit` in each app) and `npm run lint` (ESLint with the
Next 16 flat config; Next 16 removed `next lint`, so these are the only type and lint
gates the web workspace has), then builds `web/storefront` and `web/portal`. The BFF
proxy, the shared API helpers, and the host response types the two apps have in common
live in the `web/shared` workspace package (`icommerce-shared`).

The audit step is there because the Next 16 / React 19 upgrade was done to clear the
workspace's high-severity advisories. Without a gate, they come back on a transitive
bump with nothing failing.

The Node version is a single top-level `env.NODE_VERSION` value, currently `22`,
matching the Next 16 / React 19 the workspace builds on (Next 16 requires Node >= 20.9).

After both builds, this job also installs Python 3.12 and `requirements-dev.txt`,
starts the host with no `ANTHROPIC_API_KEY` (`/capabilities` reports `unconfigured`),
waits for `/readyz` to prove the engine can answer, runs `scripts/tour.py` against it
over HTTP so the store has state, starts both built
web apps against that host, and runs `scripts/pw_check.mjs` (`@playwright/test`,
headless Chromium, no API key) to assert the portal's DOM actually holds a
`.evidence.kernel` row and a `.evidence.log` row — visibly distinct by class and label
text — and that the storefront's order-history panel renders live state rather than
its unreachable-API fallback. Building this check found that the host had no CORS
middleware, so neither web app could read a response from it in a real browser at all;
`host/app.py` now allows `localhost:3000`/`:3100`.

## What required CI does not cover

The required push/pull-request jobs do not exercise a live model turn; they have no
`ANTHROPIC_API_KEY`. `pytest`, `scripts/check.py`, `scripts/denials.py`, and
`scripts/tour.py` all run against the deterministic engine and agent layers without ever
calling out to a model, so a green run proves the agent-layer gates, the engine's own
transactional refusals, the storefront/portal builds, and (via the headless check) that
both web apps render live engine state correctly — not that a live model chooses the
right tool calls. The separate `live-evals.yml` workflow supplies that non-deterministic
signal. See `docs/testing.md` for the full account of what each suite proves.
