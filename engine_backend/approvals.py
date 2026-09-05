"""The durable approval ledger: one operator's approval of one staged proposal, its
single-use apply claim, the target leases that claim takes, and the append-only event
history behind all of it.

This is control-plane state beside the engine, owned by this repository. It is what
makes an approval survive a restart, lets exactly one process claim a staged change,
and keeps two changes from mutating the same target concurrently. ``EngineStore``
owns the connection and the OS-held operation lock; this ledger owns the rows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from engine_backend.store import EngineStore


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


class ApprovalLedger:
    """Reached as ``store.approvals``; every method runs one control-plane transaction."""

    def __init__(self, store: EngineStore) -> None:
        self._store = store

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

    def record(self, change_id: str, approved_by: str, proposal_digest: str) -> None:
        """Durably record or renew approval unless an apply is in flight or complete."""
        now = self._store._now()
        connection = self._store._control_connection()
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

    def claim(
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
        now = self._store._now()
        unique_targets = sorted(set(targets or []))
        connection = self._store._control_connection()
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

    def finish_attempt(
        self,
        change_id: str,
        attempt_id: str,
        *,
        outcome: Literal["applied", "failed", "reconciliation_required"],
        error: str | None = None,
    ) -> None:
        """Finish only the attempt that owns the durable ``applying`` lease."""
        finished_at = self._store._now()
        safe_error = None if error is None else error[:1000]
        connection = self._store._control_connection()
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

    def record_for(self, change_id: str) -> dict[str, Any] | None:
        """Return one control-plane record for operator UI and reconciliation."""
        return self.records_for([change_id]).get(change_id)

    def events_for(self, change_id: str) -> list[dict[str, Any]]:
        """Return the append-only approval/apply history for one proposal."""
        return self.event_records_for([change_id]).get(change_id, [])

    def event_records_for(self, change_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Batch-read append-only approval/apply history for the operator UI."""
        if not change_ids:
            return {}
        grouped: dict[str, list[dict[str, Any]]] = {change_id: [] for change_id in change_ids}
        connection = self._store._control_connection()
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

    def recover_stale(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        *,
        stale_before: datetime,
    ) -> None:
        with self._store.merchant_operation(change_id):
            self._recover_stale(change_id, operator, proposal_digest, stale_before=stale_before)

    def _recover_stale(
        self,
        change_id: str,
        operator: str,
        proposal_digest: str,
        *,
        stale_before: datetime,
    ) -> None:
        """Move an abandoned ``applying`` claim into explicit reconciliation.

        This never retries the write and never releases its target leases. The age gate
        and OS-held operation lock prevent racing a live worker; after the transition, the
        observed-state workflow decides what actually happened.
        """
        now = self._store._now()
        stale_before_utc = stale_before.astimezone(UTC)
        detail = "operator opened reconciliation for a stale control-plane attempt"
        connection = self._store._control_connection()
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
        now = self._store._now()
        connection = self._store._control_connection()
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
        now = self._store._now()
        detail = error[:1000]
        connection = self._store._control_connection()
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
        now = self._store._now()
        connection = self._store._control_connection()
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

    def records_for(self, change_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-read control state without an operator-UI N+1 query."""
        if not change_ids:
            return {}
        connection = self._store._control_connection()
        try:
            placeholders = ",".join("?" for _ in change_ids)
            rows = connection.execute(
                f"SELECT * FROM icommerce_agent_approvals WHERE change_id IN ({placeholders})",
                tuple(change_ids),
            )
            return {row["change_id"]: dict(row) for row in rows}
        finally:
            connection.close()

    def approved_ids(self) -> set[str]:
        connection = self._store._control_connection()
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT change_id FROM icommerce_agent_approvals WHERE state = 'approved'"
                )
            }
        finally:
            connection.close()
