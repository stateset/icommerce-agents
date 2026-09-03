# Phase 2: Evidence and Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every gap identified in the post-release grade: no CI, no evals, no live smoke test, a 1,034-line module, six duplications, float money in the seed, a Next 14 pin carrying two CVEs, an unverified two-process staleness question, and inconsistent async discipline.

**Architecture:** Additive where it can be (CI, smoke, evals are new files), structural where the grade demanded it (splitting `merchant.py`, factoring shared helpers, retiring the evidence regex in favour of a typed field). No change may weaken an existing guarantee or alter a documented claim without updating the document in the same commit.

**Tech Stack:** Python 3.12, `stateset-embedded==1.28.5`, the seven `commerce-agents` packages from the pinned submodule, FastAPI, `mcp`, pytest, ruff, GitHub Actions, Next.js 16 / React 19 on Node 22.

**Spec:** `docs/superpowers/specs/2026-09-02-stateset-icommerce-agents-design.md` (Phase 1's design remains the binding authority for behavior; this plan changes structure and adds evidence, not contracts).

## Global Constraints

- Python 3.12 exactly. Node 22 for the web workspace (`nvm use 22`; 20.20.0, 22.18.0 and 23.3.0 are installed — the default shell's 18.20.8 is why Phase 1 pinned Next 14).
- `vendor/commerce-agents` is a submodule, never edited.
- Every money figure derives from an engine value, through `engine_backend/money.py`. No arithmetic in an adapter or a browser.
- Identity is never a tool argument nor a request-body field.
- No claim stronger than what the code delivers. A doc changed by a task ships in that task's commit.
- All data fictional; the store is "ACME Supply"; emails `.invalid`.
- `ruff check .` and `ruff format --check .` pass at every commit; `scripts/check.py` reports no drift.
- **Nothing in this phase may require an API key to pass.** Evals and the smoke script skip cleanly without one.
- Git identity is `domsteil <domsteil14@gmail.com>`. Never `dom@stateset.com`.
- Do NOT rebuild the venv — the engine compiles from Rust source and takes 15+ minutes.
- Baseline: 110 tests pass before this phase begins.

---

### Task 1: CI that proves the repo on a stranger's push

**Files:** Create `.github/workflows/ci.yml`, `.github/workflows/README.md`

**Interfaces:** Produces a workflow other tasks extend (Task 3 adds the eval job's skip-path, Task 7 the Node version).

- [ ] **Step 1: Write the workflow**

Jobs: `python` (matrix 3.12 and 3.13; checkout with `submodules: recursive`; install from `requirements-dev.txt`; `ruff check`, `ruff format --check`, `pytest`, `scripts/check.py`, `scripts/denials.py`), and `web` (Node 22, `npm ci`, build both workspaces). Cache pip and npm. The engine builds from sdist on runners whose glibc is older than 2.34 — set a generous timeout and say so in a comment.

- [ ] **Step 2: Validate the YAML parses and the job graph is what you intended**

Run: `.venv/bin/python -c "import yaml,sys; d=yaml.safe_load(open('.github/workflows/ci.yml')); print(sorted(d['jobs']))"`
Expected: the two job names.

- [ ] **Step 3: Dry-run every command the workflow runs, locally, in order**

Each must pass exactly as written in the YAML. A workflow whose commands were never run locally is a guess.

- [ ] **Step 4: Commit**

---

### Task 2: A live smoke conversation per role

**Files:** Create `scripts/smoke_chat.py`; Test `tests/test_smoke_chat.py`

**Interfaces:** Produces `build_turns(role) -> list[str]` and `main(argv)`; Task 3's evals reuse `build_turns`.

Read `vendor/commerce-agents/scripts/smoke_chat.py` first and follow its shape.

- [ ] **Step 1: Write the failing test** — assert `build_turns("shopping")` and `build_turns("merchant")` return non-empty prompt lists, that an unknown role raises `ValueError`, and that `main` exits 0 with a clear "no API key" message when `ANTHROPIC_API_KEY` is unset (the test must not need a key).
- [ ] **Step 2: Run it, watch it fail.**
- [ ] **Step 3: Implement.** Shopping turns: a search, a comparison, a cart add, an order question. Merchant turns: a performance question, a listing search, a staged price change, an apply attempt without approval (which must be refused). Stream events, print them, and exit non-zero if a turn errors or the refusal does not occur.
- [ ] **Step 4: Run the tests. Confirm the no-key path is exercised.**
- [ ] **Step 5: Commit.**

---

### Task 3: An eval suite for the claims only a model can satisfy

**Files:** Create `evals/__init__.py`, `evals/cases.py`, `evals/graders.py`, `evals/run.py`, `evals/README.md`; Test `tests/test_evals.py`

**Interfaces:** Produces `CASES: list[EvalCase]` (`id`, `role`, `prompt`, `grader`, `why`), `grade(case, transcript) -> EvalResult`, and `run(cases, client) -> list[EvalResult]` where `client` is injectable so tests use a fake.

The prompts carry rules the gates cannot enforce; this suite is where those are checked. Cover at least: a figure is stated only from a tool result; fenced third-party text is reported on, not obeyed; a write is confirmed only after its call succeeds; `checkout` is described as staging, not as a completed order; a merchant limitation (campaigns are unmanaged) is stated instead of a zero; a professional/medical question gets a referral.

- [ ] **Step 1: Write the failing test** — using `commerce_common.testing`'s fake client, assert each grader returns pass on a transcript that satisfies its rule and fail on one that violates it. Every grader needs both directions; a grader that cannot fail is not a grader.
- [ ] **Step 2: Run it, watch it fail.**
- [ ] **Step 3: Implement.** `run.py` skips with a clear message and exit 0 when no key is present.
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Write `evals/README.md`** — what each case checks, how to run it, and, stated plainly, **that this suite has never been executed against a live model**. Remove that sentence only when it stops being true.
- [ ] **Step 6: Commit.**

---

### Task 4: Split `merchant.py` and factor the duplications

**Files:** Create `engine_backend/custom_objects.py`, `engine_backend/listings.py`, `engine_backend/apply.py`; Modify `engine_backend/merchant.py`, `catalog.py`, `content.py`, `staging.py`, `storefront.py`

**Interfaces:** `custom_objects.py` produces `ensure_payload_type(commerce, handle, display_name)`, `read_payload(store, handle, ...)`, `write_payload(store, handle, ...)`. `listings.py` produces `title(product, variant, variant_count)`, `to_listing(row)`, `family_listing(...)`, `resolve_family_or_variant(store, id)`. `apply.py` produces `apply_change(ctx, change) -> StagedChange` with the five-kind dispatch.

Behavior must not change: this task is a pure refactor and every existing test must pass untouched.

- [ ] **Step 1: Record the baseline** — `pytest -q` (110 passing) and `git rev-parse HEAD`.
- [ ] **Step 2: Factor `custom_objects.py`** and route `catalog.py`, `content.py`, `staging.py` and `merchant.py`'s `_record_custom_object` through it. Run the suite.
- [ ] **Step 3: Factor `listings.py`** — `_title` is byte-identical in `storefront.py` and `merchant.py`; `to_family`/`_to_family_listing` and `get_product_details`/`get_listing` are structurally identical. Run the suite.
- [ ] **Step 4: Move the apply dispatch to `apply.py`.** Run the suite.
- [ ] **Step 5: Confirm `merchant.py` is under 500 lines** and no file gained responsibility that does not belong to it.
- [ ] **Step 6: Commit** (one commit per step is fine and preferred).

---

### Task 5: Structured evidence, retiring the regex coupling

**Files:** Modify `engine_backend/staging.py`, `engine_backend/apply.py`, `host/app.py`, `web/portal/lib/types.ts`; Test `tests/test_host_evidence.py`

`host/app.py` currently regex-matches prose that `merchant.py` emits, so a wording change makes evidence vanish from the portal silently.

- [ ] **Step 1: Extend the failing test** — assert the applied change carries a structured evidence field (`kind` in `kernel_receipt`/`activity_log`, plus `id`), and that `host/app.py` reads the field rather than parsing text. Watch it fail.
- [ ] **Step 2: Add `evidence: list[Evidence]` to the persisted staged-change record** (a payload field on the custom object; upstream's `StagedChange` model is not ours to change). Populate it at apply time in `apply.py`.
- [ ] **Step 3: Make `host/app.py` read the field; delete the regexes.** Keep the human-readable note.
- [ ] **Step 4: Also retire the `guardrail_notes[0]` positional JSON blob** for promotion and campaign drafts — move it to a `payload` field on the same record. The model currently sees a JSON blob presented as a guardrail note.
- [ ] **Step 5: Run the suite; rebuild the portal.**
- [ ] **Step 6: Commit.**

---

### Task 6: Seed money through the money seam

**Files:** Modify `engine_backend/seed.py`; Test `tests/test_seed_money.py`

Fourteen float literals still enter the binding at seed time. The values are binary-exact today, which is why nothing broke — that is luck, not discipline.

- [ ] **Step 1: Write the failing test** — for every seeded variant, assert `price_exact` is a two-place decimal string, and assert the seeded order total and payment amount likewise. Watch it fail (or, if it passes on today's values, add a price that is *not* binary-exact, such as `10.10`, and watch it fail then).
- [ ] **Step 2: Route every seeded money value through `money.exact`.**
- [ ] **Step 3: Run the suite** — several tests assert seeded prices; update them only where the assertion form changes, never the value.
- [ ] **Step 4: Commit.**

---

### Task 7: Next 16 / React 19 on Node 22, clearing both CVEs

**Files:** Modify `package.json`, `web/storefront/package.json`, `web/portal/package.json`, config files; Modify `docs/mapping.md`, `docs/install.md`

Phase 1 pinned Next 14.2.x because the default shell had Node 18.20.8. Node 22.18.0 is installed.

- [ ] **Step 1: Record the baseline** — `npm audit --workspace web/storefront` (expect two high) and both builds green on the current pin.
- [ ] **Step 2: Move to Node 22, upgrade to Next 16 / React 19**, matching the vendor examples' versions. Remove the root `overrides` if they are no longer needed; if they are, say why in a comment.
- [ ] **Step 3: Restore the `next.config.ts` the Next 14 pin forced to `.mjs`**, if Next 16 supports it.
- [ ] **Step 4: Build both apps; re-run `npm audit`.** Both highs must be gone. If any remain, record them in `docs/mapping.md` with the reason they cannot be cleared.
- [ ] **Step 5: Update `docs/mapping.md` and `docs/install.md`** — delete the known-deviation note and the audit findings if they are resolved; state the Node 22 requirement.
- [ ] **Step 6: Run `pytest tests/test_web_build.py -v`. Commit.**

---

### Task 8: Settle the two-process staleness question

**Files:** Create `tests/test_store_multiprocess.py`; Modify `engine_backend/store.py` docstring, `docs/mapping.md`

The WAL-index staleness fix pins one connection **per process**. Two host processes on one store file may reopen the hazard. Nothing tests it, and the docs do not say.

- [ ] **Step 1: Write the experiment first, as a script, and run it** — two processes, each an `EngineStore` on one file; one performs a direct-SQL write; assert what the other's engine handle then reads. Record the raw result in the task report before writing any test.
- [ ] **Step 2: Write the test that encodes the finding**, using `multiprocessing` and skipping cleanly if the platform cannot support it.
- [ ] **Step 3: If the hazard exists**, fix it if a bounded fix exists, and if not, document the constraint precisely in `EngineStore`'s docstring and `docs/mapping.md` — "one process per store file" is an acceptable documented constraint; an undocumented one is not.
- [ ] **Step 4: Run the suite. Commit.**

---

### Task 9: Async discipline sweep

**Files:** Modify `engine_backend/apply.py`, `engine_backend/merchant.py`, `host/app.py`

`_log_apply`, `_record_custom_object`, `execute_analysis_query` and three call sites in `host/app.py` call the engine synchronously on the event loop, bypassing the per-key lock — so a log write can run concurrently with the `store.write` that produced the thing it logs.

- [ ] **Step 1: Enumerate every `self.store.commerce.*` and `store.commerce.*` outside `store.call`/`store.write`.** List them in the report before changing any.
- [ ] **Step 2: Route each through `store.call` or `store.write`** with the lock key that matches what it touches. Where a synchronous call is genuinely correct, leave it and comment why.
- [ ] **Step 3: Run the suite** — pay attention to any test that newly deadlocks, which would indicate a lock-key collision.
- [ ] **Step 4: Commit.**

---

### Task 10: Update the documentation to the new state

**Files:** Modify `README.md`, `docs/mapping.md`, `docs/enforcement.md`, `docs/install.md`; Create `docs/testing.md`

- [ ] **Step 1: Write `docs/testing.md`** — what the suite covers, what the evals cover, what has **never been run against a live model**, and the exact commands. This is the honest-limitations page; write it as such.
- [ ] **Step 2: Update `README.md`** — CI badge, the smoke and eval commands, the Node 22 requirement. Keep the house style: plain declarative sentences, each fact once, no history.
- [ ] **Step 3: Update `docs/mapping.md`** — the new module boundaries from Task 4, the structured evidence from Task 5, the Node/Next state from Task 7, the two-process finding from Task 8.
- [ ] **Step 4: Re-read `docs/enforcement.md` against the refactored code.** Every row of the enforcement table must still name the right module and command after Task 4 moved the dispatch.
- [ ] **Step 5: Run the full verification line and `scripts/check.py`. Commit.**

---

## Self-Review

**Coverage of the graded gaps:** no CI → Task 1. No live smoke → Task 2. No evals → Task 3. 1,034-line module and six duplications → Task 4. Structured evidence → Task 5. Seed float money → Task 6. Next 14 and two CVEs → Task 7. Two-process staleness → Task 8. Async discipline → Task 9. Docs → Task 10.

**Placeholders:** none. Task 8's outcome is genuinely unknown, which is why its first step is an experiment whose raw result is recorded before any test is written — that is a method, not a TBD.

**Type consistency:** `custom_objects.py`'s three functions are consumed by Tasks 4 and 5; `listings.py`'s four by Task 4; `apply.py`'s dispatch by Tasks 5 and 9; `evals`' `CASES`/`grade`/`run` by Task 3 alone. `build_turns` is produced in Task 2 and consumed in Task 3.

**Known ceiling:** with no API key, Task 3 delivers an eval suite that has never run against a live model. Task 3 Step 5 and Task 10 Step 1 both require that limitation to be stated in the shipped documentation.
