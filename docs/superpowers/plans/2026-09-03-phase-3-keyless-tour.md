# Phase 3: The Keyless Tour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Make the repo demonstrate its own point without an API key and without a terminal. Today a stranger clones, runs `run_demo.py --web`, opens the storefront, types a message and gets a missing-key error — while the governed checkout receipt, the receipt-versus-log evidence contrast, and the three refusals are reachable only by running Python scripts and reading stdout.

**Architecture:** Nothing fake. The tour drives the same HTTP routes a human would, against the same engine, producing the same sealed receipts — it simply has no model in the loop. The web apps gain read routes so they can render live store state when the assistant is unavailable, instead of rendering nothing.

**Tech Stack:** Python 3.12, FastAPI, `stateset-embedded`, pytest; Next.js 16 / React 19 on Node 22.

**Spec:** `docs/superpowers/specs/2026-09-02-stateset-icommerce-agents-design.md` remains binding for behavior. This phase adds read surfaces and a driver; it changes no guarantee.

## Global Constraints

- **Nothing fake.** The tour calls real routes and real backends. If a step cannot be done without a model, it is not in the tour.
- **No new guarantee, and no weakened one.** No agent-reachable path may complete an order; identity never travels as a tool argument or request-body field; money derives from engine values through `engine_backend/money.py`.
- The tour must need **no API key**, and must exit non-zero if an expected outcome does not occur — it is a demo and a test.
- All data fictional; the store is "ACME Supply".
- `ruff check .` / `ruff format --check .` pass; `.venv/bin/python scripts/check.py` reports no drift; the full suite (~161 tests) passes in the **foreground**.
- **Do NOT rebuild the venv** (Rust build, 15+ minutes). Web work needs Node 22 (`source /home/dom/.nvm/nvm.sh && nvm use 22`).
- Stage files **by path**; never `git add -A`.

---

### Task 1: Read routes, the tour driver, and the denials re-run fix

**Files:** Modify `host/app.py`, `scripts/denials.py`; Create `scripts/tour.py`, `tests/test_tour.py`

**Interfaces produced:**
- `GET /shopping/cart`, `GET /shopping/orders`, `GET /merchant/changes` — session-scoped reads, same 401 gate as every other route.
- `GET /capabilities` — reports whether a model is configured (`{"assistant": "available"|"unconfigured"}`), so a browser can tell an unconfigured deployment from a broken one. It must not leak whether a key is *valid*, only whether one is present.
- `scripts/tour.py` — `main(argv) -> int`, and `run_tour(base_url, session_ids) -> TourResult`.

- [ ] **Step 1: Write the failing test.** `tests/test_tour.py` asserts the tour exits 0 with no API key set, that it produces a completed order, and that it records **both** evidence kinds — `activity_log` from a price update and `kernel_receipt` from a restock of a SKU with no inventory item. Assert on the evidence kinds, not on prose.
- [ ] **Step 2: Run it, watch it fail.**
- [ ] **Step 3: Add the read routes** to `host/app.py`. Each reads through the existing backends; none introduces a write. `GET /merchant/changes` returns pending and applied changes with their structured evidence.
- [ ] **Step 4: Write `scripts/tour.py`.** In order: open a shopping session, add a variant to the cart, place the order through the checkout route, print the sealed receipt; then open a merchant session, stage a price update, attempt the apply **without approval and show it refused**, approve, apply, print the `activity_log` evidence; then stage a restock of a SKU with no inventory item, approve, apply, print the `kernel_receipt` evidence. Narrate each step in one line. Exit non-zero if any expected outcome is missing — including if the unapproved apply *succeeds*.
- [ ] **Step 5: Fix `scripts/denials.py`'s re-run behavior.** Against an existing `denials-demo.db` it reports an idempotency conflict rather than the over-refund, because the idempotency key is fixed. Make a re-run produce the same three denials it produces on a fresh store — a per-run idempotency suffix is the obvious route; whatever you choose, add a test that runs it **twice against the same db** and asserts all three denials both times.
- [ ] **Step 6: Full suite, ruff, check.py, and `scripts/tour.py` end to end. Commit.**

---

### Task 2: The web surfaces show live state without a model

**Files:** Modify `web/storefront/`, `web/portal/` (components, `lib/api.ts`, `lib/types.ts`)

- [ ] **Step 1: On load, both apps fetch `GET /capabilities`.** When the assistant is `unconfigured`, replace the chat composer with an honest, designed state: the assistant is unavailable because no model is configured, this store's data is real, and here is what is in it. Not an error toast, not a dead input — a state someone designed.
- [ ] **Step 2: Storefront renders live state** from `GET /shopping/cart` and `GET /shopping/orders` — the bag and the order history, including an order the tour placed, with its total from the host's `*_exact` values. No arithmetic in the browser.
- [ ] **Step 3: Portal renders live state** from `GET /merchant/changes` — staged and applied changes with their evidence, keeping the existing visual distinction between a sealed kernel receipt and an activity-log id. This is the artifact the whole repo exists to show; it must be visible on first load after a tour run, with no typing.
- [ ] **Step 4: Both apps still degrade honestly when the API is down** — distinguish "API unreachable" from "assistant unconfigured". They are different problems and a reader should not confuse them.
- [ ] **Step 5: Build both on Node 22; run `tests/test_web_build.py`. Commit.**

---

### Task 3: Documentation

**Files:** Modify `README.md`, `docs/testing.md`, `scripts/run_demo.py`

- [ ] **Step 1:** `run_demo.py` gains `--tour` (or documents running `scripts/tour.py` alongside it) so one command produces a store with something to look at.
- [ ] **Step 2:** README's "What it demonstrates" section points at the tour as the way to see it without a key, in two lines. `docs/testing.md` notes the tour is a keyless end-to-end check and what it does **not** cover — it exercises the routes and the engine, never the model.
- [ ] **Step 3:** Full verification line. Commit.

## Self-Review

Covers the two gaps I named: the demo's keyless first impression (Tasks 1-2) and the `denials.py` re-run wart (Task 1 Step 5). It does **not** cover the largest gap — running the evals and smoke script against a live model — because no API key is available here; that remains the top item and one command away.
