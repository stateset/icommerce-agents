# Changelog

This project follows [Semantic Versioning](https://semver.org/). Comparison links are
the authoritative commit-level record; notable operator-visible changes are listed here.

## [Unreleased]

### Changed

- The shopper's settle route and the operator's reconciliation route complete a settled
  stablecoin payment through one path; any failure between `settled` and `completed`
  parks the payment in `reconciliation_required` instead of leaving it committing.
- Both chat routes stream through one turn helper: claim, response policy, persistence,
  and lease release are written once.
- Missing engine records (a customer behind a session or a payment, the approval record
  after an approve) are refused explicitly instead of raising attribute errors.

### Added

- `mypy` over `engine_backend/`, `host/`, and `mcp_servers/`, clean and required in CI
  and the release workflow.
- `pytest-xdist`: the suite runs in parallel by default and finishes in about two and
  a half minutes on four cores.
- Vitest coverage for the shared web package: the BFF's upstream-origin validation,
  cross-site mutation rejection, header allow-lists, and the control-request helper.

## [0.10.0] - 2026-09-05

### Changed

- The host is a package: `host/app.py` builds the deployment and owns the middlewares,
  routes live in `host/routes/` (system, sessions, shopping, stablecoin, merchant,
  refunds), request bodies in `host/schemas.py`, shared helpers in `host/context.py`,
  and every environment knob in `host/settings.py`, validated before the engine opens.
- `EngineStore` is file-backed only. The parallel in-memory implementation of
  approvals, leases, bindings, and chat locks is gone; `:memory:` is refused.
- The control-plane schema and its forward-only migrations moved to
  `engine_backend/migrations.py`.
- Host logs are JSON lines carrying the request id (`host/logs.py`).
- The two web apps share one backend-for-frontend proxy, the API helpers, and the host
  response types through the `web/shared` workspace package.
- Demo databases default to `data/` instead of the repository root.
- `pyproject.toml` declares the vendored commerce-agents packages it imports.

### Added

- A session-scoped seeded engine template for pytest; fixtures copy it instead of paying
  the engine's first-open migration cost per test, cutting the suite from about seven
  minutes to about three.
- `npm run typecheck` and `npm run lint` (ESLint with the Next 16 flat config) for the
  web workspace, required in CI and in the release workflow; Next 16 had removed
  `next lint`, leaving the previous scripts broken.
- Fast pytest environment preflight for blocked asyncio socket-pair notifications,
  with a standalone keyless diagnostic and actionable sandbox error.
- Read-only JSON release preflight for candidate evidence, exact-commit CI, and GitHub controls.
- OS-held chat-turn ownership with real paused-worker and crash-recovery coverage.
- Structured repeated live-eval reports and mandatory browser verification during GA release.
- Recorded control-plane schema migrations with forward-version refusal.
- Machine-enforced production release evidence and protected release automation.
- SPDX dependency inventory and GitHub build-provenance attestations for GA artifacts.
- Pinned CodeQL scanning and Dependabot coverage across Python, npm, Actions, and the
  Anthropic submodule.
- Human-operated stablecoin refunds through a pluggable HTTPS treasury contract, with
  digest-bound review, atomic balance reservations, idempotency, and reconciliation.

### Fixed

- Eval setup failures stop before the graded prompt; checkout scenarios now verify
  a populated engine-backed cart instead of trusting an assistant's setup claim.
- Eval streams close explicitly on early setup failure and grading completion.
- Live-eval reports checkpoint every completed case atomically; interruptions retain
  partial results, and cleanup failures cannot leave a passing report.
- Per-case cooperative deadlines bound stalled live evaluations; metadata and
  checkpoint failures close the client before propagating.
- Release gates now inspect the complete model report, candidate identity, every
  case verdict across all three runs, and its canonical artifact checksum.
- Live behavioral coverage now includes six baseline and six user-pressure cases;
  the production evidence gate requires 36/36 results across three full runs.
- Session expiries normalize to UTC; naive values are rejected before persistence,
  legacy offsets expire correctly, and concurrent renewals survive bulk cleanup.
- Principal binding snapshots cannot be mutated by in-memory callers.
- Behavioral evals reject empty/error turns, failed price sources, and prices stated
  before their supporting tool result; injection markers are checked without case bias.
- Idle session write locks and completed merchant-operation guards no longer
  accumulate indefinitely in worker memory; queued callers retain shared ownership.
- Durable principal reads no longer accumulate an unused worker-local identity cache.
- Expired-session reads cannot delete a concurrent renewal or its cart state.
- Host shutdown attempts every provider cleanup and closes the shared Claude client.
- Invalid deployment settings are rejected before network clients are allocated.
- Interrupted model streams cannot leave eagerly dispatched commerce tasks behind.
- Idle durable chat snapshots no longer accumulate without a cache limit.
- Paused merchant apply/reconciliation workers cannot be overtaken by stale recovery.
- Live-eval workflow artifacts no longer contaminate candidate cleanliness checks.
- Request cancellation no longer releases engine locks before worker writes finish.
- Lost chat heartbeats stop agent execution; interrupted claims and saves clean up leases.
- Existing session IDs cannot be reassigned to another principal, role, or store.

## [0.9.0] - 2026-09-04

### Added

- Durable shopping and merchant transcripts plus provenance state.
- Expiring, renewable cross-worker chat-turn leases and abandoned-turn recovery.
- Atomic, role-scoped request limits backed by hashed durable buckets.
- Production fail-closed rate-limit configuration and expired-session cleanup.

## [0.8.0] - 2026-09-04

### Added

- x402 v2 stablecoin checkout, durable payment journal, recovery queue, and storefront
  wallet flow.
- Authenticated same-origin web BFFs, Prometheus metrics, online backups, and hardened
  production configuration validation.

[Unreleased]: https://github.com/stateset/icommerce-agents/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/stateset/icommerce-agents/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/stateset/icommerce-agents/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/stateset/icommerce-agents/compare/v0.7.1...v0.8.0
