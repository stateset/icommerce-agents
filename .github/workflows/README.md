# CI

`ci.yml` runs on push to `master` and on every pull request. Two independent jobs;
`web` never blocks `python` — neither has a `needs:` on the other.

## `python`

Checks out the repo with `submodules: recursive` (`vendor/commerce-agents` is a git
submodule), installs `requirements-dev.txt`, and runs, in order: `ruff check .`,
`ruff format --check .`, `pytest`, `scripts/check.py` (the drift check between code
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

`stateset-embedded==1.30.0` publishes `manylinux_2_34` wheels, and `ubuntu-latest`'s
glibc satisfies that, so installing normally takes well under a minute. A runner with
an older glibc falls back to building the engine from its cargo/maturin sdist —
compiling Rust from source, which can take 15+ minutes. The job's `timeout-minutes: 30`
accounts for that fallback.

Pip is cached via `actions/setup-python`'s `cache: pip`.

## `web`

Checks out the repo (submodules too, since the web workspace depends on
`vendor/commerce-agents/examples/web-shared`), installs Node via `actions/setup-node`
with `cache: npm`, runs `npm ci`, then `npm audit --audit-level=high`, then builds
`web/storefront` and `web/portal`.

The audit step is there because the Next 16 / React 19 upgrade was done to clear the
workspace's high-severity advisories. Without a gate, they come back on a transitive
bump with nothing failing.

The Node version is a single top-level `env.NODE_VERSION` value, currently `22`,
matching the Next 16 / React 19 the workspace builds on (Next 16 requires Node >= 20.9).

After both builds, this job also installs Python 3.12 and `requirements-dev.txt`,
starts the host with no `ANTHROPIC_API_KEY` (`/capabilities` reports `unconfigured`),
runs `scripts/tour.py` against it over HTTP so the store has state, starts both built
web apps against that host, and runs `scripts/pw_check.mjs` (`@playwright/test`,
headless Chromium, no API key) to assert the portal's DOM actually holds a
`.evidence.kernel` row and a `.evidence.log` row — visibly distinct by class and label
text — and that the storefront's order-history panel renders live state rather than
its unreachable-API fallback. Building this check found that the host had no CORS
middleware, so neither web app could read a response from it in a real browser at all;
`host/app.py` now allows `localhost:3000`/`:3100`.

## What CI does not cover

No job exercises a live model turn or runs an eval suite — both need `ANTHROPIC_API_KEY`,
and CI has none. `pytest`, `scripts/check.py`, `scripts/denials.py`, and
`scripts/tour.py` all run against the deterministic engine and agent layers without ever
calling out to a model, so a green run proves the agent-layer gates, the engine's own
transactional refusals, the storefront/portal builds, and (via the headless check) that
both web apps render live engine state correctly — not that a live model chooses the
right tool calls. See `docs/testing.md` for the full account of what the suite does and
does not prove.
