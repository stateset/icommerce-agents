# Install

Python 3.12 exactly (`requires-python = ">=3.12,<3.13"` in `pyproject.toml` — the pinned
`stateset-embedded==1.28.5` wheel is built for 3.12). Node is needed only for `web/`.

```bash
git clone --recurse-submodules <this repo>
cd stateset-icommerce-agents
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If the submodule was not fetched with the clone:

```bash
git submodule update --init
```

## The glibc caveat

`stateset-embedded` publishes prebuilt wheels tagged `manylinux_2_34`. On a system with
an older glibc (before 2.34 — check with `ldd --version`), pip cannot use the wheel and
falls back to building the sdist from source with `cargo` and `maturin`, which takes
minutes rather than seconds and requires a Rust toolchain on the machine doing the
install. This is expected, not a failure; let it run.

## Node

`web/storefront` and `web/portal` are Next.js 14.2.x / React 18.3.1, deliberately one
major behind the vendor `web-shared` examples' Next 16 / React 19: Next 16 requires
Node ≥ 20.9 and refuses to build under Node 18, which is what this repo's install was
verified against (`node -v` → `v18.20.8`). Next 14.2.35 builds cleanly on Node 18.17+.
If your Node is 20.9 or newer, Next 14 still works; there is no requirement to upgrade.

```bash
npm install
npm run build --workspace web/storefront
npm run build --workspace web/portal
```

## Verify

```bash
ruff check . && ruff format --check . && pytest && python scripts/check.py
```

`scripts/denials.py` needs no `ANTHROPIC_API_KEY` — it exercises the agent-layer gates
and the kernel directly, not a model call:

```bash
python scripts/denials.py
```

Running the host or the Agent SDK console for a live chat turn does need
`ANTHROPIC_API_KEY` set in the environment.
