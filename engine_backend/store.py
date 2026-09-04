"""One engine handle per store file, plus the server-held session→principal binding."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel
from stateset_embedded import Commerce

T = TypeVar("T")


@dataclass(frozen=True)
class ApprovalClaim:
    """Result of atomically trying to spend one durable approval."""

    attempt_id: str | None = None
    refusal: (
        Literal[
            "missing",
            "different_operator",
            "already_claimed",
            "already_applied",
            "reconciliation_required",
            "target_claimed",
            "proposal_changed",
        ]
        | None
    ) = None
    blocked_target: str | None = None


class PrincipalBinding(BaseModel):
    """Who a session acts for. Set by the host at session start; never a tool argument."""

    session_id: str
    subject_id: str
    kind: Literal["customer", "operator"]
    store_id: str
    authenticated_subject: str | None = None
    expires_at: datetime | None = None


class EngineStore:
    """One engine handle, one pinned connection, and the session→principal bindings.

    The connection :meth:`_pin_connection` holds is per *process*, and what it holds is
    a share of the WAL index -- see that method for the mechanism and for why holding a
    file descriptor alone is not enough. Two host processes on one store file
    (``scripts/run_demo.py`` alongside a separately launched MCP server) are measured in
    ``tests/test_store_multiprocess.py``, and **a second process is covered precisely
    because it pins too.**

    The staleness is in-process in origin, which is the opposite of the obvious guess.
    POSIX advisory locks are held per process, so the lock a *second* process takes on
    the WAL index really does block this process from tearing it down; two SQLite
    libraries inside *one* process do not block each other at all. That is why a writer
    process that opens, writes and exits leaves a reader's handle correct, while the same
    open-and-close inside the reader's own process destroys it. An *unpinned* process
    that makes a transient ``sqlite3`` connection of its own -- one opened and closed
    around a single statement, which is exactly what :meth:`write_sql` does on every
    apply -- loses its ``Commerce`` handle's view of every later ``write_sql``,
    permanently, while disk stays correct. What matters is the open-and-close, not
    whether the statement is a read or a write: :meth:`readonly_sql` is *not* an instance
    of it, because it caches one connection per thread and holds it for that thread's
    life.

    So the pin is not redundant across processes and is not merely a single-process
    concern: it is the only thing standing between a deployment and silently stale
    prices. Each process that opens a store gets its own, which is why the per-process
    scope is the right scope rather than a gap.

    One caution for anyone reproducing this: an incidental extra connection anywhere in
    the harness masks the whole effect, which is how it stayed hidden here twice. A
    reader that performs an engine binding write before its read, or a diagnostic
    connection left open beside the store, reads correct values with the pin off and
    proves nothing. A third way to be misled is to reproduce it on one machine only: the
    symptom depends on the SQLite version behind Python's ``sqlite3`` (see
    :meth:`_pin_connection`), so a green run proves nothing about another host.

    Three operational facts about a second process matter:

    - Direct-SQL writes are serialized against each other by an ``asyncio`` lock, which
      reaches only as far as one process. Two processes writing at once are ordered by
      SQLite's own file lock and wait on the ``busy_timeout`` :meth:`write_sql` sets, not
      by that lock.
    - Approval, its single-use apply claim, and target leases are durable SQLite state.
      They survive restarts, exactly one process can claim a staged id, and different
      changes cannot mutate the same target concurrently. A crashed or ambiguous apply
      deliberately retains both its visible state and its leases for reconciliation.
    - Principal and session→cart bindings are durable adapter tables. File-backed
      principal reads always consult durable state so revocation is immediately visible
      across workers; only in-memory deployments use ``self._bindings`` as authority.
      Role-scoped chat transcripts and provenance state are durable too; an expiring
      database lease admits only one turn across all workers.
    """

    def __init__(self, db_path: str, store_id: str = "store:acme") -> None:
        self.db_path = db_path
        self.store_id = store_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._bindings: dict[str, PrincipalBinding] = {}
        self._sql = threading.local()
        self._memory_approvals: dict[str, dict[str, Any]] = {}
        self._memory_target_leases: dict[str, tuple[str, str]] = {}
        self._memory_approval_events: list[dict[str, Any]] = []
        self._memory_chat_leases: dict[tuple[str, str], str] = {}
        self._memory_approvals_lock = threading.Lock()
        self._memory_chat_leases_lock = threading.Lock()
        # Create adapter-owned tables before opening the embedded engine. Opening and
        # closing Python's SQLite afterwards can invalidate the engine binding's WAL
        # view on some SQLite builds (the pin below exists for the same reason).
        self._ensure_control_schema()
        self.commerce = Commerce(db_path)
        self._pin = self._pin_connection()

    def _ensure_control_schema(self) -> None:
        """Create the adapter-owned durable control plane beside engine tables.

        This happens before the WAL pin is opened. Later control operations use
        transient connections while the pin keeps Python's SQLite from unlinking the
        WAL index underneath the engine's embedded SQLite handle.
        """
        if self.db_path == ":memory:":
            return
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
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
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'icommerce_agent_approvals'"
            ).fetchone()[0]
            if (
                "reconciliation_required" not in schema
                or "'reconciling'" not in schema
                or "'resolved'" not in schema
            ):
                # Upgrade databases created by the first durable-ledger revision. A
                # SQLite CHECK cannot be altered in place, so rebuild transactionally.
                connection.execute(
                    "ALTER TABLE icommerce_agent_approvals "
                    "RENAME TO icommerce_agent_approvals_legacy"
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
                    for row in connection.execute(
                        "PRAGMA table_info(icommerce_agent_approvals_legacy)"
                    )
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
                connection.execute(
                    "ALTER TABLE icommerce_agent_approvals ADD COLUMN proposal_digest TEXT"
                )
            for column in ("resolved_at", "resolved_by", "resolution"):
                if column not in approval_columns:
                    connection.execute(
                        f"ALTER TABLE icommerce_agent_approvals ADD COLUMN {column} TEXT"
                    )
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
            connection.commit()
        finally:
            connection.close()

    def _control_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _insert_approval_event(
        connection: sqlite3.Connection,
        *,
        change_id: str,
        event: str,
        operator: str,
        occurred_at: str,
        proposal_digest: str | None,
        attempt_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO icommerce_agent_approval_events (
                change_id, event, operator, occurred_at,
                proposal_digest, attempt_id, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change_id,
                event,
                operator,
                occurred_at,
                proposal_digest,
                attempt_id,
                detail,
            ),
        )

    def record_approval(self, change_id: str, approved_by: str, proposal_digest: str) -> None:
        """Durably record or renew approval unless an apply is in flight or complete."""
        now = self._now()
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                current = self._memory_approvals.get(change_id)
                if current and current["state"] in (
                    "applying",
                    "applied",
                    "reconciliation_required",
                    "reconciling",
                    "resolved",
                ):
                    raise ValueError(f"change {change_id} is already {current['state']}")
                self._memory_approvals[change_id] = {
                    "change_id": change_id,
                    "approved_by": approved_by,
                    "approved_at": now,
                    "state": "approved",
                    "attempt_id": None,
                    "claimed_at": None,
                    "finished_at": None,
                    "last_error": None,
                    "proposal_digest": proposal_digest,
                    "resolved_at": None,
                    "resolved_by": None,
                    "resolution": None,
                }
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": "approved",
                        "operator": approved_by,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": None,
                        "detail": None,
                    }
                )
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if row is not None and row["state"] in (
                "applying",
                "applied",
                "reconciliation_required",
                "reconciling",
                "resolved",
            ):
                raise ValueError(f"change {change_id} is already {row['state']}")
            connection.execute(
                """
                INSERT INTO icommerce_agent_approvals (
                    change_id, approved_by, approved_at, state,
                    attempt_id, claimed_at, finished_at, last_error, proposal_digest
                ) VALUES (?, ?, ?, 'approved', NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(change_id) DO UPDATE SET
                    approved_by = excluded.approved_by,
                    approved_at = excluded.approved_at,
                    state = 'approved',
                    attempt_id = NULL,
                    claimed_at = NULL,
                    finished_at = NULL,
                    last_error = NULL,
                    proposal_digest = excluded.proposal_digest,
                    resolved_at = NULL,
                    resolved_by = NULL,
                    resolution = NULL
                """,
                (change_id, approved_by, now, proposal_digest),
            )
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event="approved",
                operator=approved_by,
                occurred_at=now,
                proposal_digest=proposal_digest,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_approval(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        targets: list[str] | None = None,
    ) -> ApprovalClaim:
        """Atomically move an approval from ``approved`` to ``applying``.

        The conditional transition is the cross-process single-claim gate. The caller
        must finish the returned attempt as either ``applied`` or ``failed``.
        """
        attempt_id = f"attempt-{uuid4().hex}"
        now = self._now()
        unique_targets = sorted(set(targets or []))
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                refusal = self._approval_refusal(row, operator, proposal_digest)
                if refusal is not None:
                    return ApprovalClaim(refusal=refusal)
                for target in unique_targets:
                    if target in self._memory_target_leases:
                        return ApprovalClaim(refusal="target_claimed", blocked_target=target)
                row.update(state="applying", attempt_id=attempt_id, claimed_at=now)
                for target in unique_targets:
                    self._memory_target_leases[target] = (change_id, attempt_id)
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": "claimed",
                        "operator": operator,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": attempt_id,
                        "detail": None,
                    }
                )
                return ApprovalClaim(attempt_id=attempt_id)

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            refusal = self._approval_refusal(
                dict(row) if row is not None else None, operator, proposal_digest
            )
            if refusal is not None:
                connection.rollback()
                return ApprovalClaim(refusal=refusal)
            for target in unique_targets:
                lease = connection.execute(
                    "SELECT change_id FROM icommerce_agent_target_leases WHERE target = ?",
                    (target,),
                ).fetchone()
                if lease is not None:
                    connection.rollback()
                    return ApprovalClaim(refusal="target_claimed", blocked_target=target)
            connection.executemany(
                """
                INSERT INTO icommerce_agent_target_leases (
                    target, change_id, attempt_id, claimed_at
                ) VALUES (?, ?, ?, ?)
                """,
                [(target, change_id, attempt_id, now) for target in unique_targets],
            )
            changed = connection.execute(
                """
                UPDATE icommerce_agent_approvals
                SET state = 'applying', attempt_id = ?, claimed_at = ?,
                    finished_at = NULL, last_error = NULL
                WHERE change_id = ? AND state = 'approved' AND approved_by = ?
                """,
                (attempt_id, now, change_id, operator),
            ).rowcount
            if changed != 1:  # defensive: BEGIN IMMEDIATE should make this unreachable
                connection.rollback()
                return ApprovalClaim(refusal="already_claimed")
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event="claimed",
                operator=operator,
                occurred_at=now,
                proposal_digest=proposal_digest,
                attempt_id=attempt_id,
            )
            connection.commit()
            return ApprovalClaim(attempt_id=attempt_id)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _approval_refusal(
        row: dict[str, Any] | None, operator: str, proposal_digest: str
    ) -> (
        Literal[
            "missing",
            "different_operator",
            "already_claimed",
            "already_applied",
            "reconciliation_required",
            "target_claimed",
            "proposal_changed",
        ]
        | None
    ):
        if row is None or row["state"] == "failed":
            return "missing"
        if row["approved_by"] != operator:
            return "different_operator"
        if row.get("proposal_digest") != proposal_digest:
            return "proposal_changed"
        if row["state"] == "applying":
            return "already_claimed"
        if row["state"] in ("applied", "resolved"):
            return "already_applied"
        if row["state"] in ("reconciliation_required", "reconciling"):
            return "reconciliation_required"
        return None

    def finish_approval_attempt(
        self,
        change_id: str,
        attempt_id: str,
        *,
        outcome: Literal["applied", "failed", "reconciliation_required"],
        error: str | None = None,
    ) -> None:
        """Finish only the attempt that owns the durable ``applying`` lease."""
        finished_at = self._now()
        safe_error = None if error is None else error[:1000]
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                if row is None or row.get("attempt_id") != attempt_id:
                    raise RuntimeError(f"approval attempt {attempt_id} no longer owns {change_id}")
                row.update(state=outcome, finished_at=finished_at, last_error=safe_error)
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": outcome,
                        "operator": row["approved_by"],
                        "occurred_at": finished_at,
                        "proposal_digest": row.get("proposal_digest"),
                        "attempt_id": attempt_id,
                        "detail": safe_error,
                    }
                )
                if outcome != "reconciliation_required":
                    for target, owner in list(self._memory_target_leases.items()):
                        if owner == (change_id, attempt_id):
                            del self._memory_target_leases[target]
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE icommerce_agent_approvals
                SET state = ?, finished_at = ?, last_error = ?
                WHERE change_id = ? AND state = 'applying' AND attempt_id = ?
                """,
                (outcome, finished_at, safe_error, change_id, attempt_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"approval attempt {attempt_id} no longer owns {change_id}")
            if outcome != "reconciliation_required":
                connection.execute(
                    "DELETE FROM icommerce_agent_target_leases "
                    "WHERE change_id = ? AND attempt_id = ?",
                    (change_id, attempt_id),
                )
            row = connection.execute(
                "SELECT approved_by, proposal_digest FROM icommerce_agent_approvals "
                "WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event=outcome,
                operator=row["approved_by"],
                occurred_at=finished_at,
                proposal_digest=row["proposal_digest"],
                attempt_id=attempt_id,
                detail=safe_error,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approval_record(self, change_id: str) -> dict[str, Any] | None:
        """Return one control-plane record for operator UI and reconciliation."""
        return self.approval_records([change_id]).get(change_id)

    def approval_events(self, change_id: str) -> list[dict[str, Any]]:
        """Return the append-only approval/apply history for one proposal."""
        return self.approval_event_records([change_id]).get(change_id, [])

    def approval_event_records(self, change_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Batch-read append-only approval/apply history for the operator UI."""
        if not change_ids:
            return {}
        grouped = {change_id: [] for change_id in change_ids}
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                for event in self._memory_approval_events:
                    if event["change_id"] in grouped:
                        grouped[event["change_id"]].append(dict(event))
                return grouped
        connection = self._control_connection()
        try:
            placeholders = ",".join("?" for _ in change_ids)
            rows = connection.execute(
                "SELECT * FROM icommerce_agent_approval_events "
                f"WHERE change_id IN ({placeholders}) ORDER BY event_id",
                tuple(change_ids),
            )
            for row in rows:
                grouped[row["change_id"]].append(dict(row))
            return grouped
        finally:
            connection.close()

    def recover_stale_approval(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        *,
        stale_before: datetime,
    ) -> None:
        """Move an abandoned ``applying`` claim into explicit reconciliation.

        This never retries the write and never releases its target leases. The age gate
        prevents an operator from racing a healthy worker; after the transition, the
        observed-state workflow decides what actually happened.
        """
        now = self._now()
        stale_before_utc = stale_before.astimezone(UTC)
        detail = "operator opened reconciliation for a stale control-plane attempt"
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                if row is None or row["state"] not in ("applying", "reconciling"):
                    raise ValueError(f"change {change_id} has no active attempt to recover")
                if row.get("proposal_digest") != proposal_digest:
                    raise ValueError(f"change {change_id} proposal digest changed")
                timestamp = (
                    row.get("claimed_at") if row["state"] == "applying" else row.get("resolved_at")
                )
                if not timestamp:
                    raise ValueError(f"change {change_id} attempt has no recovery timestamp")
                claimed_at = datetime.fromisoformat(timestamp)
                if claimed_at > stale_before_utc:
                    raise ValueError(f"change {change_id} attempt is still within its lease")
                row.update(
                    state="reconciliation_required",
                    finished_at=now,
                    last_error=detail,
                    resolved_at=None,
                    resolved_by=None,
                    resolution=None,
                )
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": "reconciliation_required",
                        "operator": operator,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": row.get("attempt_id"),
                        "detail": detail,
                    }
                )
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if row is None or row["state"] not in ("applying", "reconciling"):
                raise ValueError(f"change {change_id} has no active attempt to recover")
            if row["proposal_digest"] != proposal_digest:
                raise ValueError(f"change {change_id} proposal digest changed")
            timestamp = row["claimed_at"] if row["state"] == "applying" else row["resolved_at"]
            if not timestamp:
                raise ValueError(f"change {change_id} attempt has no recovery timestamp")
            claimed_at = datetime.fromisoformat(timestamp)
            if claimed_at > stale_before_utc:
                raise ValueError(f"change {change_id} attempt is still within its lease")
            changed = connection.execute(
                """
                UPDATE icommerce_agent_approvals
                SET state = 'reconciliation_required', finished_at = ?, last_error = ?,
                    resolved_at = NULL, resolved_by = NULL, resolution = NULL
                WHERE change_id = ? AND state = ? AND attempt_id = ?
                """,
                (now, detail, change_id, row["state"], row["attempt_id"]),
            ).rowcount
            if changed != 1:
                raise ValueError(f"change {change_id} apply attempt changed concurrently")
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event="reconciliation_required",
                operator=operator,
                occurred_at=now,
                proposal_digest=proposal_digest,
                attempt_id=row["attempt_id"],
                detail=detail,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_reconciliation(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        resolution: Literal["confirmed_applied", "accepted_current_state"],
    ) -> None:
        """Atomically grant one operator ownership of a reconciliation decision."""
        now = self._now()
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                if row is None or row["state"] != "reconciliation_required":
                    raise ValueError(f"change {change_id} does not require reconciliation")
                if row.get("proposal_digest") != proposal_digest:
                    raise ValueError(f"change {change_id} proposal digest changed")
                row.update(
                    state="reconciling",
                    resolved_at=now,
                    resolved_by=operator,
                    resolution=resolution,
                )
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": f"reconciliation_claimed:{resolution}",
                        "operator": operator,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": row.get("attempt_id"),
                        "detail": None,
                    }
                )
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if row is None or row["state"] != "reconciliation_required":
                raise ValueError(f"change {change_id} does not require reconciliation")
            if row["proposal_digest"] != proposal_digest:
                raise ValueError(f"change {change_id} proposal digest changed")
            connection.execute(
                """
                UPDATE icommerce_agent_approvals
                SET state = 'reconciling', resolved_at = ?, resolved_by = ?, resolution = ?
                WHERE change_id = ? AND state = 'reconciliation_required'
                """,
                (now, operator, resolution, change_id),
            )
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event=f"reconciliation_claimed:{resolution}",
                operator=operator,
                occurred_at=now,
                proposal_digest=proposal_digest,
                attempt_id=row["attempt_id"],
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def abort_reconciliation(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        resolution: Literal["confirmed_applied", "accepted_current_state"],
        error: str,
    ) -> None:
        """Return a normally failed metadata update to reconciliation-required."""
        now = self._now()
        detail = error[:1000]
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                if (
                    row is None
                    or row["state"] != "reconciling"
                    or row.get("resolved_by") != operator
                    or row.get("resolution") != resolution
                    or row.get("proposal_digest") != proposal_digest
                ):
                    raise ValueError(f"change {change_id} reconciliation claim is not owned")
                row.update(
                    state="reconciliation_required",
                    resolved_at=None,
                    resolved_by=None,
                    resolution=None,
                    last_error=detail,
                )
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": "reconciliation_failed",
                        "operator": operator,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": row.get("attempt_id"),
                        "detail": detail,
                    }
                )
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "reconciling"
                or row["resolved_by"] != operator
                or row["resolution"] != resolution
                or row["proposal_digest"] != proposal_digest
            ):
                raise ValueError(f"change {change_id} reconciliation claim is not owned")
            connection.execute(
                """
                UPDATE icommerce_agent_approvals
                SET state = 'reconciliation_required', resolved_at = NULL,
                    resolved_by = NULL, resolution = NULL, last_error = ?
                WHERE change_id = ? AND state = 'reconciling'
                """,
                (detail, change_id),
            )
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event="reconciliation_failed",
                operator=operator,
                occurred_at=now,
                proposal_digest=proposal_digest,
                attempt_id=row["attempt_id"],
                detail=detail,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_reconciliation(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        resolution: Literal["confirmed_applied", "accepted_current_state"],
    ) -> None:
        """Finish the reconciliation claim owned by ``operator`` and release leases."""
        now = self._now()
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                row = self._memory_approvals.get(change_id)
                if (
                    row is None
                    or row["state"] != "reconciling"
                    or row.get("resolved_by") != operator
                    or row.get("resolution") != resolution
                    or row.get("proposal_digest") != proposal_digest
                ):
                    raise ValueError(f"change {change_id} reconciliation claim is not owned")
                row.update(state="resolved", resolved_at=now)
                attempt_id = row.get("attempt_id")
                for target, owner in list(self._memory_target_leases.items()):
                    if owner == (change_id, attempt_id):
                        del self._memory_target_leases[target]
                self._memory_approval_events.append(
                    {
                        "change_id": change_id,
                        "event": f"reconciled:{resolution}",
                        "operator": operator,
                        "occurred_at": now,
                        "proposal_digest": proposal_digest,
                        "attempt_id": attempt_id,
                        "detail": None,
                    }
                )
            return

        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM icommerce_agent_approvals WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != "reconciling"
                or row["resolved_by"] != operator
                or row["resolution"] != resolution
                or row["proposal_digest"] != proposal_digest
            ):
                raise ValueError(f"change {change_id} reconciliation claim is not owned")
            changed = connection.execute(
                "UPDATE icommerce_agent_approvals SET state = 'resolved', resolved_at = ? "
                "WHERE change_id = ? AND state = 'reconciling'",
                (now, change_id),
            ).rowcount
            if changed != 1:
                raise ValueError(f"change {change_id} reconciliation changed concurrently")
            connection.execute(
                "DELETE FROM icommerce_agent_target_leases WHERE change_id = ? AND attempt_id = ?",
                (change_id, row["attempt_id"]),
            )
            self._insert_approval_event(
                connection,
                change_id=change_id,
                event=f"reconciled:{resolution}",
                operator=operator,
                occurred_at=now,
                proposal_digest=proposal_digest,
                attempt_id=row["attempt_id"],
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def approval_records(self, change_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-read control state without an operator-UI N+1 query."""
        if not change_ids:
            return {}
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                return {
                    change_id: dict(self._memory_approvals[change_id])
                    for change_id in change_ids
                    if change_id in self._memory_approvals
                }
        connection = self._control_connection()
        try:
            placeholders = ",".join("?" for _ in change_ids)
            rows = connection.execute(
                f"SELECT * FROM icommerce_agent_approvals WHERE change_id IN ({placeholders})",
                tuple(change_ids),
            )
            return {row["change_id"]: dict(row) for row in rows}
        finally:
            connection.close()

    def approved_change_ids(self) -> set[str]:
        if self.db_path == ":memory:":
            with self._memory_approvals_lock:
                return {
                    change_id
                    for change_id, row in self._memory_approvals.items()
                    if row["state"] == "approved"
                }
        connection = self._control_connection()
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT change_id FROM icommerce_agent_approvals WHERE state = 'approved'"
                )
            }
        finally:
            connection.close()

    async def call(self, fn: Callable[[Commerce], T]) -> T:
        return await asyncio.to_thread(fn, self.commerce)

    async def write(self, session_key: str, fn: Callable[[Commerce], T]) -> T:
        async with self.serialized(session_key):
            return await asyncio.to_thread(fn, self.commerce)

    @asynccontextmanager
    async def serialized(self, operation_key: str):
        """Serialize a compound in-process operation under ``operation_key``.

        ``write`` protects one binding call, but some correctness boundaries span
        multiple reads and writes. Those callers take a purpose-specific outer lock
        and retain their normal, different-key write locks inside it.
        """
        lock = self._locks.setdefault(operation_key, asyncio.Lock())
        async with lock:
            yield

    async def write_sql(self, sql: str, params: tuple[Any, ...]) -> None:
        """The only direct-SQL write path in this repo, and the only safe one.

        Three fields have no mutator in the engine's Python binding at all
        (``product_variants.price``, ``products.status``, ``products.description`` -- no
        ``products.update``, no variant-price setter), so they are written with a single
        parameterized ``UPDATE`` on a connection this method opens itself. Direct-SQL
        writes are serialized against each other under one lock key; ``busy_timeout``
        makes contention with a concurrent binding write wait rather than raise.

        This lives on the store rather than in ``merchant.py`` because a direct-SQL write
        is only sound in combination with the connection pinned in :meth:`_pin_connection`
        -- see that method for what goes wrong without it -- and putting the two in one
        class is what makes it impossible to add a direct-SQL write that forgets.
        """

        def body() -> None:
            connection = sqlite3.connect(self.db_path, timeout=30)
            try:
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.execute(sql, params)
                connection.commit()
            finally:
                connection.close()

        lock = self._locks.setdefault("direct_sql", asyncio.Lock())
        async with lock:
            await asyncio.to_thread(body)

    def _pin_connection(self) -> sqlite3.Connection | None:
        """One read-only connection that joins the WAL index and then holds a share of it.

        This repo has two independent SQLite libraries open on one file: the engine's
        own (bundled in the Rust extension) and Python's ``sqlite3``, which
        :meth:`write_sql` and :meth:`readonly_sql` use for the reads and writes the
        binding does not expose. On a WAL database they coordinate through the
        shared-memory WAL index, the ``-shm`` file beside the ``-wal``. When a connection
        closes and SQLite believes it is the last user of that index, it checkpoints and
        **unlinks** ``-wal`` and ``-shm``. A handle that still has the unlinked index
        mapped keeps reading it: it is now a private copy of a file no one else will ever
        write to, so every WAL frame appended afterwards -- through the ``-wal`` the next
        connection creates -- is invisible to it, for the rest of its life, while the row
        on disk is correct.

        "Believes it is the last user" is where the two libraries part company. That
        belief rests on a POSIX advisory lock, and POSIX advisory locks are held per
        *process*: a lock taken by the engine's SQLite does not block Python's
        ``sqlite3`` in the same process from taking it exclusively, so
        :meth:`write_sql`'s connection unlinks the index out from under a live
        ``Commerce`` handle every time it closes. It is only within one library that
        SQLite tracks its own open connections and refuses. Hence the shape of this
        method: the pin must be a connection of *Python's* library, and it must actually
        be a user of the WAL index, not merely a descriptor on the file.

        That distinction is the whole fix. ``SELECT 1`` touches no table, so it opens no
        read transaction and never maps ``-shm``; a pin that only ran ``SELECT 1`` was
        counted by nothing and prevented nothing. Reading ``sqlite_schema`` opens a real
        read transaction, which maps the index and leaves this connection registered as a
        user of it for as long as the connection is open -- while releasing the read
        transaction itself, so checkpointing is not blocked and the WAL does not grow
        without bound.

        The symptoms of getting this wrong are not subtle, and all three were observed
        here: the engine's ``Commerce`` handle serving a pre-write price for the rest of
        its life while the row on disk is correct (so the *second* applied price update
        of a process silently never reaches the storefront -- the host holds one
        ``Commerce``, and ``catalog.list_variants`` resolves through
        ``get_variant_by_sku``); a read-only connection returning a row from an entirely
        different table; and ``PRAGMA wal_checkpoint(TRUNCATE)`` leaving the engine
        handle raising ``disk I/O error``.

        Whether the ``SELECT 1`` pin *appeared* to work depended on the SQLite version
        behind Python's ``sqlite3``, which is why this was believed settled twice.
        Measured on one machine, against the same engine build (``stateset-embedded``
        1.28.5 bundles SQLite 3.46.0 in both its sdist and its ``manylinux`` wheel), four
        successive ``write_sql`` price writes with a transient reader between them:
        stale from the second write on with Python's SQLite at 3.45.1, 3.46.0 and 3.47.1;
        correct with 3.50.4. The repo's own machine ships 3.50.4 and CI ships an older
        one, so the bug shipped and only CI ever saw it. With the ``sqlite_schema`` read
        below, all four versions track every write.

        The connection is never read from again: its only job is to hold that share. An
        in-memory store has no file to pin and no ``readonly_sql`` either, so it gets
        ``None``.
        """
        if self.db_path == ":memory:":
            return None
        connection = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
        )
        # A real table read, run to completion: it maps the WAL index (which `SELECT 1`
        # does not) and then ends its read transaction (which `fetchone` on an unfinished
        # statement would not), leaving this connection a registered user of the index
        # without holding a read mark against the checkpointer.
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchall()
        return connection

    def readonly_sql(self) -> sqlite3.Connection:
        """A read-only connection for the reads the binding does not expose.

        Listed in docs/mapping.md; every use is a single parameterized SELECT.

        One connection per thread, kept for the life of that thread. Callers reach this
        from two different places -- a worker thread, inside ``store.call(...)``
        (``catalog.list_variants``), and directly on the event loop
        (``merchant.execute_analysis_query``) -- and a ``sqlite3.Connection`` and its
        cursors are not safe to use from two threads concurrently. Sharing one
        connection would need every *use* serialized, not just the lazy connect; giving
        each thread its own removes the sharing instead, which is why
        ``check_same_thread`` is left at its default here.
        """
        if self.db_path == ":memory:":
            raise RuntimeError("a read-only connection needs a file-backed store, not :memory:")
        connection: sqlite3.Connection | None = getattr(self._sql, "connection", None)
        if connection is None:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self._sql.connection = connection
        return connection

    def bind(
        self,
        session_id: str,
        subject_id: str,
        kind: Literal["customer", "operator"],
        *,
        authenticated_subject: str | None = None,
        expires_at: datetime | None = None,
    ) -> PrincipalBinding:
        binding = PrincipalBinding(
            session_id=session_id,
            subject_id=subject_id,
            kind=kind,
            store_id=self.store_id,
            authenticated_subject=authenticated_subject,
            expires_at=expires_at,
        )
        self._bindings[session_id] = binding
        if self.db_path != ":memory:":
            connection = self._control_connection()
            try:
                connection.execute(
                    """
                    INSERT INTO icommerce_agent_sessions (
                        session_id, subject_id, kind, store_id,
                        authenticated_subject, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        subject_id = excluded.subject_id,
                        kind = excluded.kind,
                        store_id = excluded.store_id,
                        authenticated_subject = excluded.authenticated_subject,
                        expires_at = excluded.expires_at
                    """,
                    (
                        session_id,
                        subject_id,
                        kind,
                        self.store_id,
                        authenticated_subject,
                        expires_at.isoformat() if expires_at is not None else None,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        return binding

    def binding(self, session_id: str) -> PrincipalBinding:
        if self.db_path != ":memory:":
            connection = self._control_connection()
            try:
                row = connection.execute(
                    "SELECT * FROM icommerce_agent_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                self._bindings.pop(session_id, None)
                raise KeyError(session_id)
            binding = PrincipalBinding.model_validate(dict(row))
            self._bindings[session_id] = binding
        else:
            binding = self._bindings[session_id]
        if binding.expires_at is not None and binding.expires_at <= datetime.now(UTC):
            self.unbind(session_id)
            raise KeyError(session_id)
        return binding

    def unbind(self, session_id: str) -> None:
        """Revoke a durable session binding; missing ids are already revoked."""
        self._bindings.pop(session_id, None)
        if self.db_path != ":memory:":
            connection = self._control_connection()
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "DELETE FROM icommerce_agent_sessions WHERE session_id = ?", (session_id,)
                )
                connection.commit()
            finally:
                connection.close()

    def cleanup_expired_sessions(self) -> int:
        """Delete expired identity/workflow state and its chat/cart children."""
        if self.db_path == ":memory:":
            now = datetime.now(UTC)
            expired = [
                session_id
                for session_id, binding in self._bindings.items()
                if binding.expires_at is not None and binding.expires_at <= now
            ]
            for session_id in expired:
                self.unbind(session_id)
            return len(expired)
        connection = self._control_connection()
        try:
            now = datetime.now(UTC)
            connection.execute("PRAGMA foreign_keys = ON")
            cursor = connection.execute(
                "DELETE FROM icommerce_agent_sessions "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now.isoformat(),),
            )
            connection.commit()
            for session_id, binding in list(self._bindings.items()):
                if binding.expires_at is not None and binding.expires_at <= now:
                    self._bindings.pop(session_id, None)
            return cursor.rowcount
        finally:
            connection.close()

    def session_cart_id(self, session_id: str) -> str | None:
        if self.db_path == ":memory:":
            return None
        connection = self._control_connection()
        try:
            row = connection.execute(
                "SELECT cart_id FROM icommerce_agent_session_carts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return str(row["cart_id"]) if row is not None else None
        finally:
            connection.close()

    def claim_session_cart(self, session_id: str, cart_id: str) -> str:
        """Install one durable cart per session and return the winning cart id.

        Two workers may both create an empty candidate, but the unique session key means
        every subsequent read/write converges on one winner before either adds a line.
        """
        if self.db_path == ":memory:":
            return cart_id
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO icommerce_agent_session_carts "
                "(session_id, cart_id, created_at) VALUES (?, ?, ?)",
                (session_id, cart_id, self._now()),
            )
            row = connection.execute(
                "SELECT cart_id FROM icommerce_agent_session_carts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("session cart claim disappeared")
            return str(row["cart_id"])
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_chat_session(
        self, session_id: str, role: str, state_json: str, messages_json: str
    ) -> None:
        if self.db_path == ":memory:":
            return
        connection = self._control_connection()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO icommerce_agent_chat_sessions "
                "(session_id, role, state_json, messages_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, state_json, messages_json, self._now()),
            )
            connection.commit()
        finally:
            connection.close()

    def claim_chat_turn(
        self, session_id: str, role: str, lease_seconds: int
    ) -> tuple[str, dict[str, Any]] | None:
        """Atomically own a chat turn and return the latest durable snapshot."""
        owner = uuid4().hex
        if self.db_path == ":memory:":
            key = (session_id, role)
            with self._memory_chat_leases_lock:
                if key in self._memory_chat_leases:
                    return None
                self._memory_chat_leases[key] = owner
            return owner, {}
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions "
                "SET lease_owner = ?, lease_expires_at = ? "
                "WHERE session_id = ? AND role = ? "
                "AND (lease_owner IS NULL OR lease_expires_at <= ?)",
                (owner, expires.isoformat(), session_id, role, now.isoformat()),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT state_json, messages_json, revision "
                "FROM icommerce_agent_chat_sessions WHERE session_id = ? AND role = ?",
                (session_id, role),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("claimed chat session disappeared")
            return owner, dict(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_chat_turn(self, session_id: str, role: str, owner: str, lease_seconds: int) -> None:
        if self.db_path == ":memory:":
            with self._memory_chat_leases_lock:
                if self._memory_chat_leases.get((session_id, role)) != owner:
                    raise RuntimeError("chat turn lease was lost")
            return
        connection = self._control_connection()
        try:
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions SET lease_expires_at = ? "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (
                    (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                    session_id,
                    role,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("chat turn lease was lost")
            connection.commit()
        finally:
            connection.close()

    def finish_chat_turn(
        self,
        session_id: str,
        role: str,
        owner: str,
        state_json: str,
        messages_json: str,
    ) -> None:
        if self.db_path == ":memory:":
            with self._memory_chat_leases_lock:
                if self._memory_chat_leases.get((session_id, role)) != owner:
                    raise RuntimeError("chat turn lease was lost before persistence")
                self._memory_chat_leases.pop((session_id, role), None)
            return
        connection = self._control_connection()
        try:
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions SET state_json = ?, "
                "messages_json = ?, revision = revision + 1, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (state_json, messages_json, self._now(), session_id, role, owner),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("chat turn lease was lost before persistence")
            connection.commit()
        finally:
            connection.close()

    def release_chat_turn(self, session_id: str, role: str, owner: str) -> None:
        if self.db_path == ":memory:":
            with self._memory_chat_leases_lock:
                if self._memory_chat_leases.get((session_id, role)) == owner:
                    self._memory_chat_leases.pop((session_id, role), None)
            return
        connection = self._control_connection()
        try:
            connection.execute(
                "UPDATE icommerce_agent_chat_sessions "
                "SET lease_owner = NULL, lease_expires_at = NULL "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (session_id, role, owner),
            )
            connection.commit()
        finally:
            connection.close()

    def consume_rate_limit(self, principal: str, limit: int, window_start: int) -> bool:
        """Consume one fixed-minute allowance without storing a principal identifier."""
        if limit <= 0 or self.db_path == ":memory:":
            return True
        key_hash = hashlib.sha256(principal.encode()).hexdigest()
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO icommerce_agent_rate_limits "
                "(key_hash, window_start, request_count) VALUES (?, ?, 1) "
                "ON CONFLICT(key_hash, window_start) DO UPDATE "
                "SET request_count = request_count + 1",
                (key_hash, window_start),
            )
            count = connection.execute(
                "SELECT request_count FROM icommerce_agent_rate_limits "
                "WHERE key_hash = ? AND window_start = ?",
                (key_hash, window_start),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM icommerce_agent_rate_limits WHERE window_start < ?",
                (window_start - 3600,),
            )
            connection.commit()
            return count <= limit
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
