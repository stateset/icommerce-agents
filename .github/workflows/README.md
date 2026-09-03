# CI

`ci.yml` runs on push to `master` and on every pull request. Two independent jobs;
`web` never blocks `python` — neither has a `needs:` on the other.

## `python`

Checks out the repo with `submodules: recursive` (`vendor/commerce-agents` is a git
submodule), installs `requirements-dev.txt`, and runs, in order: `ruff check .`,
`ruff format --check .`, `pytest`, `scripts/check.py` (the drift check between code
and documentation), and `scripts/denials.py` (the three end-to-end refusals: a cart
write blocked for lack of provenance, an apply blocked for lack of approval, and an
over-refund the engine itself rejects inside the transaction).

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
with `cache: npm`, runs `npm ci`, then builds `web/storefront` and `web/portal`.

The Node version is a single top-level `env.NODE_VERSION` value, currently `22`,
matching the Next 16 / React 19 the workspace builds on (Next 16 requires Node >= 20.9).

## What CI does not cover

No job exercises a live model turn or runs an eval suite — both need `ANTHROPIC_API_KEY`,
and CI has none. `pytest`, `scripts/check.py`, and `scripts/denials.py` all run against
the deterministic engine and agent layers without ever calling out to a model, so a green
run proves the agent-layer gates, the engine's own transactional refusals, and the
storefront/portal builds — not that a live model chooses the right tool calls. See
`docs/testing.md` for the full account of what the suite does and does not prove.
