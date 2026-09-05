# Production operations

Run the host with `ICOMMERCE_ENVIRONMENT=production`; the startup checks described in
[`install.md`](install.md) turn unsafe edge configuration into a hard failure.

Terminate TLS in a trusted ingress, cap request-body size and connection duration
there, and configure graceful shutdown longer than the slowest permitted agent turn.
Keep the SQLite database on a single host's durable local filesystem; the host supports
multiple worker processes sharing that file, not multiple machines sharing SQLite over
a network filesystem. Use an external database implementation before horizontally
scaling the write plane across machines.

Run the host with ASGI lifespan enabled (Uvicorn's default): shutdown closes the
shared Claude client and both payment-provider clients, attempting the remaining
cleanup callbacks even if one fails. Cleanup errors remain visible to the server;
this is not a substitute for draining active requests before terminating a worker.

## Turn ownership and worker recovery

Deployment configs disable eager tool dispatch: commerce calls begin after the
model response completes and run through the upstream awaited tool join. The
pinned upstream eager dispatcher cancels its tasks without awaiting termination
on an interrupted model stream; using the joined path keeps engine execution
inside the host's ownership boundary. This trades overlap between generation
and tool execution for predictable cancellation. Live evals use the same policy.

Each role's durable-session registry caches up to 128 session snapshots by
default. Active turns are pinned and may temporarily exceed that limit; idle
snapshots are evicted and restored from SQLite on the next claim. This bounds
idle transcript retention in a worker without truncating durable history or
losing provenance. In-memory test stores do not evict their only copy of state.
Durable principal bindings are read directly from SQLite without a worker-local
identity cache, so revocations remain visible across workers. An expired read
is rejected, but its cleanup cannot delete a concurrently renewed binding.
Expiry inputs must include a timezone and are stored in UTC. Cleanup handles older
offset-bearing records by instant, including sub-millisecond boundaries; a legacy
naive or malformed expiry denies session access and is retained for operator repair
instead of guessing a timezone or deleting its workflow state. Principal snapshots
are immutable; renewal goes through the store's identity-preserving `bind` operation.
In-process write locks retire when no owner or queued caller retains them;
merchant guards retain only active proposal IDs. This bounds idle lock metadata,
not the OS lock files below, which must remain in place while workers run.

Each file-backed chat turn holds both a durable database lease and an OS `flock`
under `<database-path>.turn-locks/`. A stopped or stalled process keeps its lock,
even after lease expiry; competing turns return busy instead of taking over.
After the owning process exits, the OS releases the lock and another worker can
recover once the database lease has expired. Graceful completion saves state
and releases both. Cancellation drains synchronous engine work before release.

To recover a wedged worker, drain its traffic and terminate that specific process;
do not delete a lock file to force takeover. Lock files contain no data and their
names are hashes, but their inodes are part of the ownership protocol. Never unlink
or replace the `.turn-locks` directory while any worker is running. Exclude it from
backups; remove old files only during an all-workers-stopped maintenance window.
Use one canonical database path on a local filesystem; hard-linked aliases and
network filesystems are unsupported. Construct stores after worker creation, not
before forking processes that would inherit lock descriptors.

The first upgrade to OS-held turn ownership requires draining and stopping **all**
old workers before starting new ones. Older workers do not honor these locks.
The database lease remains necessary after an unclean exit, so immediate recovery
may wait up to `ICOMMERCE_CHAT_LEASE_SECONDS`.

Merchant apply, reconciliation resolution, and stale-attempt recovery now share
an OS-held operation lock per proposal. An aged attempt cannot be recovered while
its worker still owns that operation, including during a process pause. Drain and
terminate the specific wedged worker first. After a crash, recovery still retains
target leases and requires an observed-state reconciliation decision; it never
automatically retries a possibly completed write. The all-workers-stopped upgrade
requirement also applies when introducing this merchant ownership guard.

## Backup and restore

Create backups with SQLite's online snapshot API rather than copying the database file
while WAL writes may be in flight:

```bash
python scripts/backup_store.py /var/lib/icommerce/store.db /backups/store-2026-09-04.db
```

Encrypt the live database volume and every backup. The command refuses to overwrite a
destination, writes in the destination directory, runs `PRAGMA integrity_check`, and
atomically publishes the verified snapshot. Restrict backup access as tightly as the
live database and set retention from your privacy and accounting policy.

Test restore regularly: stop traffic to a disposable environment, copy one verified
snapshot into its configured database path, start exactly one host, require `/readyz`
to pass, then verify an order, an approval event, and any unresolved stablecoin payment
against the source runbook. Never rehearse a restore over the live store.

## Minimum alerts

- sustained non-2xx rate and p95/p99 request latency from `/metrics`;
- sustained HTTP 429 responses, indicating a client loop, abuse, or a limit that needs
  deliberate capacity review rather than an automatic increase;
- any stablecoin `reconciliation_required` transition or unresolved queue age;
- rejected kernel commands, stale apply recovery, and reconciliation activity;
- `/readyz` failure, disk-space pressure, WAL growth, and failed backups;
- live-eval regression or required CI failure on the deployed revision.

The stablecoin runbook is in [`stablecoin-checkout.md`](stablecoin-checkout.md). Payment
reconciliation is a privileged, audited break-glass action and requires independent
facilitator plus chain/RPC evidence.

## Logs

The host writes JSON lines to stderr: one object per record with `ts`, `level`,
`logger`, `message`, and, for anything emitted while serving a request, the
`request_id` the client saw in `X-Request-Id`. `scripts/run_demo.py` starts uvicorn
with `--log-config config/uvicorn-logging.json`, which routes uvicorn's own startup and
access records through the same formatter, so a deployment that starts uvicorn itself
should pass the same flag. `ICOMMERCE_LOG_LEVEL` sets the level (default `INFO`).
