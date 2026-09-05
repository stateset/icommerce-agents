"""The adapter-owned control-plane schema, applied beside the engine's own tables.

Every table here is owned by this repository, not by ``stateset-embedded``: approval
ledgers, target leases, stablecoin payment and refund journals, session bindings, chat
transcripts, and rate-limit buckets. ``EngineStore`` opens one transaction and calls
:func:`upgrade_control_schema` before the engine handle exists, so the migration runs on
a plain ``sqlite3`` connection and never touches the WAL index the pin protects.

Migrations are forward-only. A database recorded at a version newer than
:data:`CONTROL_SCHEMA_VERSION` is refused rather than downgraded.
"""

from __future__ import annotations

import sqlite3

CONTROL_SCHEMA_VERSION = 2


def upgrade_control_schema(connection: sqlite3.Connection, now: str) -> None:
    """Create or upgrade the control-plane tables inside the caller's transaction.

    ``now`` is the ISO timestamp recorded against any migration applied by this call.
    The caller owns ``BEGIN``/``COMMIT``/``ROLLBACK``.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = connection.execute(
        "SELECT MAX(version) FROM icommerce_agent_schema_migrations"
    ).fetchone()
    installed_version = int(row[0] or 0)
    if installed_version > CONTROL_SCHEMA_VERSION:
        raise RuntimeError(
            "control schema is newer than this application "
            f"({installed_version} > {CONTROL_SCHEMA_VERSION})"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_approvals (
            change_id TEXT PRIMARY KEY,
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'approved', 'applying', 'applied', 'failed',
                    'reconciliation_required', 'reconciling', 'resolved'
                )
            ),
            attempt_id TEXT,
            claimed_at TEXT,
            finished_at TEXT,
            last_error TEXT,
            proposal_digest TEXT,
            resolved_at TEXT,
            resolved_by TEXT,
            resolution TEXT
        )
        """
    )
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'icommerce_agent_approvals'"
    ).fetchone()[0]
    if (
        "reconciliation_required" not in schema
        or "'reconciling'" not in schema
        or "'resolved'" not in schema
    ):
        # Upgrade databases created by the first durable-ledger revision. A
        # SQLite CHECK cannot be altered in place, so rebuild transactionally.
        connection.execute(
            "ALTER TABLE icommerce_agent_approvals RENAME TO icommerce_agent_approvals_legacy"
        )
        connection.execute(
            """
            CREATE TABLE icommerce_agent_approvals (
                change_id TEXT PRIMARY KEY,
                approved_by TEXT NOT NULL,
                approved_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'approved', 'applying', 'applied', 'failed',
                        'reconciliation_required', 'reconciling', 'resolved'
                    )
                ),
                attempt_id TEXT,
                claimed_at TEXT,
                finished_at TEXT,
                last_error TEXT,
                proposal_digest TEXT,
                resolved_at TEXT,
                resolved_by TEXT,
                resolution TEXT
            )
            """
        )
        legacy_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(icommerce_agent_approvals_legacy)")
        }
        current_columns = [
            "change_id",
            "approved_by",
            "approved_at",
            "state",
            "attempt_id",
            "claimed_at",
            "finished_at",
            "last_error",
            "proposal_digest",
            "resolved_at",
            "resolved_by",
            "resolution",
        ]
        copied_columns = [column for column in current_columns if column in legacy_columns]
        names = ", ".join(copied_columns)
        connection.execute(
            f"INSERT INTO icommerce_agent_approvals ({names}) "
            f"SELECT {names} FROM icommerce_agent_approvals_legacy"
        )
        connection.execute("DROP TABLE icommerce_agent_approvals_legacy")
    approval_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(icommerce_agent_approvals)")
    }
    if "proposal_digest" not in approval_columns:
        connection.execute("ALTER TABLE icommerce_agent_approvals ADD COLUMN proposal_digest TEXT")
    for column in ("resolved_at", "resolved_by", "resolution"):
        if column not in approval_columns:
            connection.execute(f"ALTER TABLE icommerce_agent_approvals ADD COLUMN {column} TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_target_leases (
            target TEXT PRIMARY KEY,
            change_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_approval_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            change_id TEXT NOT NULL,
            event TEXT NOT NULL,
            operator TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            proposal_digest TEXT,
            attempt_id TEXT,
            detail TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_approval_events_change "
        "ON icommerce_agent_approval_events(change_id, event_id)"
    )
    # x402 settles outside the embedded engine, so its hand-off state must be
    # durable beside the engine before the engine connection is opened.  The
    # row is both a replay barrier and the recovery record for the dangerous
    # interval between an on-chain settlement and ``checkout.commit``.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_stablecoin_payments (
            payment_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            cart_id TEXT NOT NULL,
            cart_digest TEXT NOT NULL,
            quote_digest TEXT NOT NULL UNIQUE,
            amount TEXT NOT NULL,
            amount_atomic TEXT NOT NULL,
            currency TEXT NOT NULL,
            asset_symbol TEXT NOT NULL,
            asset_address TEXT NOT NULL,
            asset_decimals INTEGER NOT NULL,
            network TEXT NOT NULL,
            pay_to TEXT NOT NULL,
            payer_address TEXT NOT NULL,
            shipping_address_json TEXT NOT NULL,
            payment_requirements_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'quoted', 'verifying', 'verified', 'settling',
                    'settled', 'checkout_committing', 'completed',
                    'failed', 'expired', 'reconciliation_required'
                )
            ),
            expires_at TEXT NOT NULL,
            payment_payload_hash TEXT UNIQUE,
            transaction_hash TEXT UNIQUE,
            order_number TEXT,
            checkout_receipt_json TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_stablecoin_payments_session "
        "ON icommerce_stablecoin_payments(session_id, created_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_stablecoin_payment_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            event TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            detail TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_stablecoin_payment_events_payment "
        "ON icommerce_stablecoin_payment_events(payment_id, event_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_sessions (
            session_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('customer', 'operator')),
            store_id TEXT NOT NULL,
            authenticated_subject TEXT,
            expires_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_session_carts (
            session_id TEXT PRIMARY KEY,
            cart_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES icommerce_agent_sessions(session_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_chat_sessions (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('shopping', 'merchant')),
            state_json TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(session_id, role),
            FOREIGN KEY(session_id) REFERENCES icommerce_agent_sessions(session_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS icommerce_agent_rate_limits (
            key_hash TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            request_count INTEGER NOT NULL,
            PRIMARY KEY(key_hash, window_start)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_icommerce_agent_sessions_expires_at "
        "ON icommerce_agent_sessions(expires_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_icommerce_agent_rate_limits_window_start "
        "ON icommerce_agent_rate_limits(window_start)"
    )
    if installed_version < 1:
        connection.execute(
            "INSERT INTO icommerce_agent_schema_migrations "
            "(version, name, applied_at) VALUES (?, ?, ?)",
            (
                1,
                "baseline-v0.9-control-plane",
                now,
            ),
        )
    if installed_version < 2:
        connection.execute(
            """
            CREATE TABLE icommerce_stablecoin_refunds (
                refund_id TEXT PRIMARY KEY,
                payment_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                amount TEXT NOT NULL,
                amount_atomic TEXT NOT NULL,
                proposal_digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                operator TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'submitting', 'completed', 'failed',
                        'reconciliation_required'
                    )
                ),
                transaction_hash TEXT UNIQUE,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(payment_id)
                    REFERENCES icommerce_stablecoin_payments(payment_id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_stablecoin_refunds_payment "
            "ON icommerce_stablecoin_refunds(payment_id, created_at)"
        )
        connection.execute(
            """
            CREATE TABLE icommerce_stablecoin_refund_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                refund_id TEXT NOT NULL,
                event TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX idx_stablecoin_refund_events_refund "
            "ON icommerce_stablecoin_refund_events(refund_id, event_id)"
        )
        connection.execute(
            "INSERT INTO icommerce_agent_schema_migrations "
            "(version, name, applied_at) VALUES (?, ?, ?)",
            (2, "stablecoin-refund-ledger", now),
        )
