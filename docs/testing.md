# Testing — what is covered, and what is not

**No test in this repository runs a live model turn.** `scripts/smoke_chat.py` and
`evals/` exist to do that, and neither has ever been executed against a live model,
because no `ANTHROPIC_API_KEY` is available in this environment or in CI. CI therefore
proves the code — the agent-layer gates, the engine's own transactional refusals, the
staging/apply/evidence pipeline, the web builds — and not the agent's behavior. Read
this page before trusting a green run to mean more than that.

## The ~145 pytest tests: what they cover

One file per module in `engine_backend/`, `host/`, and `mcp_servers/`, plus the suites
listed below. All of it runs against `EngineMerchant`/`EngineStorefront` over a real,
seeded `stateset-embedded` engine instance and, where a route is involved, an in-process
ASGI client (`httpx.AsyncClient` against `host.app.create_app`) — never a mock of the
engine, and never a model.

- The agent-layer gates and guardrails (`shopping_agent.gates`, `merchant_agent.gates`,
  `merchant_agent.changes.check_guardrails`) — every denial path, checked directly by
  calling the gate, not by asking a model to trigger it.
- The engine-layer kernel check on the five commands this deployment governs
  (`config/kernel-policy.json`), including the over-refund rejection inside the
  transaction.
- The `stage_*` / `apply_change` pipeline end to end: staging, guardrail re-check at
  apply time, approval gating, the direct-SQL write fallbacks, and the structured
  `Evidence` (`kind`/`id`) a change persists (`tests/test_host_evidence.py`,
  `tests/test_staging.py`, `tests/test_merchant_writes.py`).
- Money: the seeded catalog's prices and the seeded order/payment amounts pass through
  `engine_backend/money.py`'s exact-decimal seam with no precision gained or lost
  (`tests/test_seed_money.py`).
- The cross-process staleness finding for the store's pinned connection, in both the
  single-process and two-process cases, including the pin-off failure mode
  (`tests/test_store.py`, `tests/test_store_multiprocess.py`).
- `evals/graders.py`'s six graders, both directions (pass and fail) for each, against
  hand-built transcripts (`tests/test_evals.py`) — this tests that the graders grade
  correctly, not that a model's actual output passes them. Separately, every literal a
  case looks for is checked against a seeded store and the real read tools run through
  the real executors, so a case pinned to a figure or a marker this deployment never
  emits fails here rather than sitting in the suite as one that can never pass.
- `scripts/smoke_chat.py`'s turn-building and per-turn expectation logic
  (`tests/test_smoke_chat.py`), including one end-to-end run through a fake, scripted
  client that never calls the expected tool — confirming the check actually fails when
  it should, rather than passing vacuously.
- The Next.js builds for `web/storefront` and `web/portal` (`tests/test_web_build.py`).

## What the suite does not cover

- **Whether a real model, given these tools and this prompt, behaves correctly.** Every
  test above either calls the gate/engine/backend directly or drives a scripted fake
  client. None of it sends a request to `AsyncAnthropic`.
- Whether the model states a price only from a tool result, reports on a fenced
  instruction instead of obeying it, describes checkout as staging rather than
  completion, gives a medical referral alongside a product, confirms a write only after
  success, or states the campaign limitation instead of fabricating a number —
  `evals/`'s six cases exist to check exactly these six rules, and none of them has run.
- Whether the model's own `apply_change` tool call is refused by the host when no
  approval exists — only the host's refusal logic is exercised here (by
  `tests/test_merchant_writes.py` and friends), not whether a live model attempts the
  call, retries around a refusal, or misreports the outcome to the user.
- Anything about prompt wording, tool-description clarity, or skill content actually
  landing with a model, since nothing here sends either to one.

## `scripts/smoke_chat.py` and `evals/`: written, never run live

Both scripts check for `ANTHROPIC_API_KEY` before doing anything else and, with no key
set, print a message and exit 0 without constructing an agent or a model client:

```bash
$ python scripts/smoke_chat.py
No ANTHROPIC_API_KEY set -- skipping the live smoke conversation. This script has never
been run against a live model in this environment; set ANTHROPIC_API_KEY to exercise it.

$ python -m evals.run
No ANTHROPIC_API_KEY set -- skipping the eval suite. This suite has never been run against
a live model in this environment; set ANTHROPIC_API_KEY to exercise it.
```

`scripts/smoke_chat.py` drives one scripted conversation per role (shopping: search,
compare, add to cart, check order status; merchant: a snapshot question, a listing
search, a staged price change, then an apply attempt with no host approval that the
script fails on if it succeeds). `evals/` runs six graded cases, one per rule in
`vendor/commerce-agents/docs/safety.md`'s "still asked of the model" list. Both exist
so that setting `ANTHROPIC_API_KEY` and running them is the way to close this gap — not
so that their presence closes it on its own. See `evals/README.md` for the case list and
grading detail.

## Exact commands

```bash
# Python: the full suite, ruff, and the drift check
ruff check . && ruff format --check . && pytest && python scripts/check.py

# The three end-to-end refusals, no API key needed
python scripts/denials.py

# The live smoke conversations and the eval suite -- need ANTHROPIC_API_KEY, otherwise skip and exit 0
python scripts/smoke_chat.py
python -m evals.run

# Web builds -- need Node >= 20.9 (Next 16 requirement)
nvm use 22   # or any Node >= 20.9
npm install
npm audit --audit-level=high
npm run build --workspace web/storefront
npm run build --workspace web/portal
```

## `tests/test_web_build.py` under concurrent load

This test shells out to `npm run build` for each workspace. Several Next.js builds
running at once on the same machine (for example, this test running alongside another
`npm run build` invoked separately, or two CI-like processes sharing one runner) can
fail from resource contention — out-of-memory kills, a `.next` cache race, or a
timeout — rather than from an actual code defect. If it fails only when something else
is building at the same time, and passes on a clean, single build, that is this known
issue, not a regression to chase.

The test also skips, rather than fails, in two situations that are not defects either:
`node_modules` absent (`npm install` has not run) and Node older than 20.9 — the
version Next 16 requires. A skip on Node 18 names the required version explicitly so a
reader hitting it knows to `nvm use 22` rather than debug a build error.
