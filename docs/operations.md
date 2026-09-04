# Production operations

Run the host with `ICOMMERCE_ENVIRONMENT=production`; the startup checks described in
[`install.md`](install.md) turn unsafe edge configuration into a hard failure.

## Backup and restore

Create backups with SQLite's online snapshot API rather than copying the database file
while WAL writes may be in flight:

```bash
python scripts/backup_store.py /var/lib/icommerce/store.db /backups/store-2026-09-04.db
```

The command refuses to overwrite a destination, writes in the destination directory,
runs `PRAGMA integrity_check`, and atomically publishes the verified snapshot. Encrypt
backup storage, restrict access as tightly as the live database, and set retention from
your privacy and accounting policy.

Test restore regularly: stop traffic to a disposable environment, copy one verified
snapshot into its configured database path, start exactly one host, require `/readyz`
to pass, then verify an order, an approval event, and any unresolved stablecoin payment
against the source runbook. Never rehearse a restore over the live store.

## Minimum alerts

- sustained non-2xx rate and p95/p99 request latency from `/metrics`;
- any stablecoin `reconciliation_required` transition or unresolved queue age;
- rejected kernel commands, stale apply recovery, and reconciliation activity;
- `/readyz` failure, disk-space pressure, WAL growth, and failed backups;
- live-eval regression or required CI failure on the deployed revision.

The stablecoin runbook is in [`stablecoin-checkout.md`](stablecoin-checkout.md). Payment
reconciliation is a privileged, audited break-glass action and requires independent
facilitator plus chain/RPC evidence.
