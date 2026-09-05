# Claude commerce agents / StateSet integration review

Review date: 2026-09-05. Scope: the Messages API and MCP adapters, engine
execution boundary, merchant apply path, and durable chat lifecycle. This is a
targeted code review, not a production certification or a new live-model eval.

## Findings addressed

1. **High: cancellation released engine locks before writes stopped.**
   `asyncio.to_thread` does not stop its worker when its caller is cancelled.
   Previously `EngineStore.write` and `write_sql` released their locks immediately,
   allowing a subsequent operation to overlap the still-running write. The store
   now waits for synchronous work to finish before propagating cancellation.
   `call` uses the same boundary so compound operations retain their outer lock
   until an in-flight binding call exits. Repeated cancellation is covered too.

2. **High: heartbeat failure did not stop agent execution.**
   A failed lease renewal previously surfaced only when the turn finished; the
   agent could keep issuing tools after losing ownership. Both chat routes now
   monitor the heartbeat while consuming agent events, cancel pending agent work
   on failure, and close the iterator. Lease renewal also runs frequently enough
   for short leases. This stops further execution once failure is observed; it
   cannot undo a synchronous engine call already in progress.

3. **Medium: interrupted claims and persistence could strand chat leases.**
   A cancelled claim could acquire a lease in its worker thread after the caller
   had exited. In-memory claim failures also leaked the local turn lock. Claims
   now drain and release the exact acquired owner on cancellation, and finish
   shields transcript/provenance persistence and lease cleanup from interruption.

Regression coverage lives in `tests/test_cancellation.py`. It exercises binding
writes, direct SQL, compound-operation locks, repeated cancellation, late lease
acquisition, claim failures, persistence on disconnect, and heartbeat failure.

## Architecture observations and remaining boundaries

- Preserve the two approval gates: the upstream executor's session mark and the
  engine adapter's durable, digest-bound approval claim. MCP correctly exposes
  staging and apply without an approval tool.
- Preserve the distinction between sealed kernel receipts and activity logs.
  General merchandising writes still bypass the kernel, as documented in
  `enforcement.md`; this change does not expand engine policy coverage.
- Chat claims now hold an OS file lock as well as a database lease. A paused
  process retains ownership; a dead process releases its lock automatically.
  `tests/test_turn_locks.py` stops a real worker, expires its database lease,
  verifies takeover is refused, kills it, and verifies recovery. This closes the
  paused-worker takeover gap within the supported single-host filesystem model.
  It is not distributed fencing for network filesystems or multiple hosts.
- Session identity is immutable: reconnects can refresh expiry for the same
  principal, but cannot change the customer/operator, role, store, or OIDC subject.
  This prevents a reused MCP session identifier from exposing an earlier
  principal's cart or transcript to a newly configured principal.
- Merchant apply and reconciliation resolution also retain an OS-held operation
  lock. Stale recovery acquires the same lock before changing control state, so an
  old timestamp cannot permit recovery while the earlier operation is still alive.
  Crash recovery retains target leases until an operator resolves the outcome.
- Cancellation may now take as long as an already-started synchronous engine call.
  This retains serialization and does not promise rollback. Existing ambiguous
  merchant-apply outcomes still require reconciliation.
- Model-dependent behavior still needs the existing live eval suite. Deterministic
  tests do not establish new Claude behavioral scores or payment-provider evidence.

## Resolved environment finding: eval setup integration

`tests/test_eval_setup.py::test_populated_real_cart_allows_grading` stalled inside a
restricted sandbox while waiting on `EngineStore.write`. Asyncio debug logging showed
`PermissionError: [Errno 1] Operation not permitted` in `_write_to_self`: the sandbox
denied the socket send that wakes the event loop when a worker thread finishes, so
completed engine work looked hung. The same test completes in seconds outside that
sandbox. This is an environment cause, not a serialization or cancellation defect, and
no protection was bypassed.

`scripts/runtime_check.py` now checks nonblocking socket-pair delivery without importing
the engine, opening a listener, or running an event loop. Pytest runs it at session
startup and reports an actionable error when notifications are blocked. Cooperative
deadlines deliberately drain already-started engine writes, so use an outer process
deadline (for example `timeout 180s pytest ...`) when diagnosing other stalls.
