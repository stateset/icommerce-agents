# Production operations

Run the host with `ICOMMERCE_ENVIRONMENT=production`; the startup checks described in
[`install.md`](install.md) turn unsafe edge configuration into a hard failure.

Terminate TLS in a trusted ingress, cap request-body size and connection duration
there, and configure graceful shutdown longer than the slowest permitted agent turn.
Keep the SQLite database on a single host's durable local filesystem; the host supports
multiple worker processes sharing that file, not multiple machines sharing SQLite over
a network filesystem. Use an external database implementation before horizontally
scaling the write plane across machines.

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
