"use client";

import { useEffect, useState } from "react";
import {
  approveChange,
  fetchReconciliation,
  resolveReconciliation,
  startReconciliation,
} from "../../lib/api";
import type { ReconciliationDetail, StagedChange } from "../../lib/types";
import { Evidence } from "./Evidence";
import { StablecoinPayments } from "./StablecoinPayments";

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "--";
  return String(value);
}

export function ChangesPanel({
  changes,
  onRefresh,
  stablecoinAvailable = false,
  stablecoinRefundsAvailable = false,
  sessionId = null,
}: {
  changes: Record<string, StagedChange>;
  onRefresh: () => Promise<void>;
  stablecoinAvailable?: boolean;
  stablecoinRefundsAvailable?: boolean;
  sessionId?: string | null;
}) {
  const [approving, setApproving] = useState<string | null>(null);
  const [approvedIds, setApprovedIds] = useState<Set<string>>(new Set());
  const [reconciliations, setReconciliations] = useState<
    Record<string, ReconciliationDetail>
  >({});
  const [resolving, setResolving] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Recovery eligibility compares a server timestamp with wall-clock time. Reading the
  // clock during render is impure, so the clock lives in state and ticks on an interval.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);
  const list = Object.values(changes).sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  );

  async function approve(change: StagedChange) {
    if (!change.proposal_digest) return;
    setApproving(change.change_id);
    setErrors((current) => ({ ...current, [change.change_id]: "" }));
    try {
      const result = await approveChange(
        change.change_id,
        change.proposal_digest,
      );
      if (result.ok) {
        setApprovedIds((prev) => new Set(prev).add(change.change_id));
        await onRefresh();
      } else {
        setErrors((current) => ({
          ...current,
          [change.change_id]: result.error,
        }));
      }
    } finally {
      setApproving(null);
    }
  }

  async function inspect(changeId: string) {
    const result = await fetchReconciliation(changeId);
    if (result.ok) {
      setReconciliations((current) => ({
        ...current,
        [changeId]: result.data,
      }));
      setErrors((current) => ({ ...current, [changeId]: "" }));
    } else {
      setErrors((current) => ({ ...current, [changeId]: result.error }));
    }
  }

  async function recover(change: StagedChange) {
    if (!change.proposal_digest) return;
    setResolving(change.change_id);
    setErrors((current) => ({ ...current, [change.change_id]: "" }));
    try {
      const result = await startReconciliation(
        change.change_id,
        change.proposal_digest,
      );
      if (result.ok) {
        await onRefresh();
        await inspect(change.change_id);
      } else {
        setErrors((current) => ({
          ...current,
          [change.change_id]: result.error,
        }));
      }
    } finally {
      setResolving(null);
    }
  }

  async function resolve(
    changeId: string,
    detail: ReconciliationDetail,
    resolution: "confirmed_applied" | "accepted_current_state",
  ) {
    setResolving(changeId);
    setErrors((current) => ({ ...current, [changeId]: "" }));
    try {
      const result = await resolveReconciliation(
        changeId,
        detail.proposal_digest,
        resolution,
      );
      if (result.ok) {
        await onRefresh();
      } else {
        setErrors((current) => ({ ...current, [changeId]: result.error }));
      }
    } finally {
      setResolving(null);
    }
  }

  return (
    <aside className="changes-col">
      <div className="changes-header">
        <h2>Staged changes</h2>
        <p>Approve here, then ask the assistant to apply it.</p>
      </div>
      <StablecoinPayments
        enabled={stablecoinAvailable}
        refundsEnabled={stablecoinRefundsAvailable}
        sessionId={sessionId}
      />
      {list.length === 0 ? (
        <div className="changes-empty">
          Nothing staged yet. Ask the assistant to draft a price change, a
          restock, or a listing update and it will land here.
        </div>
      ) : (
        <div className="changes-list">
          {list.map((change) => {
            const control = change.apply_control;
            const controlState = control?.state;
            const approved =
              approvedIds.has(change.change_id) || controlState === "approved";
            const applying = controlState === "applying";
            const reconciling = controlState === "reconciling";
            const needsReconciliation =
              controlState === "reconciliation_required";
            const approvalBlocked =
              applying || reconciling || needsReconciliation;
            const reconciliation = reconciliations[change.change_id];
            const recoveryAvailable =
              (applying || reconciling) &&
              change.recovery_available_at !== null &&
              change.recovery_available_at !== undefined &&
              Date.parse(change.recovery_available_at) <= now;
            return (
              <div className="change-card" key={change.change_id}>
                <div className="kind">{change.kind.replace(/_/g, " ")}</div>
                <div className="summary">{change.summary}</div>
                {change.proposal_digest ? (
                  <code
                    className="proposal-digest"
                    title={change.proposal_digest}
                  >
                    Reviewed proposal: {change.proposal_digest}
                  </code>
                ) : null}
                <div className="items">
                  {change.items.map((item, index) => (
                    <div className="item-line" key={index}>
                      <span>
                        {item.target} · {item.field}
                      </span>
                      <span>
                        {formatValue(item.before)} -&gt;{" "}
                        {formatValue(item.after)}
                      </span>
                    </div>
                  ))}
                </div>
                <span className={`status-tag ${change.status}`}>
                  {change.status}
                </span>
                {change.status === "staged" && controlState ? (
                  <span className={`status-tag control-${controlState}`}>
                    {controlState.replace(/_/g, " ")}
                  </span>
                ) : null}
                {change.status === "staged" ? (
                  <button
                    type="button"
                    className="approve-btn"
                    disabled={
                      !change.proposal_digest ||
                      approving === change.change_id ||
                      approved ||
                      approvalBlocked
                    }
                    onClick={() => approve(change)}
                  >
                    {reconciling
                      ? "Resolution in progress"
                      : applying
                        ? "Apply in progress"
                        : needsReconciliation
                          ? "Reconciliation required"
                          : approved
                            ? "Approved -- apply it via chat"
                            : approving === change.change_id
                              ? "Approving..."
                              : "Approve"}
                  </button>
                ) : null}
                {change.status === "staged" && approved ? (
                  <p className="apply-hint">
                    Ask the assistant to apply {change.change_id} to complete
                    the write.
                  </p>
                ) : null}
                {change.status === "staged" && approvalBlocked ? (
                  <p
                    className={`control-note ${needsReconciliation ? "danger" : ""}`}
                  >
                    {needsReconciliation
                      ? "The write outcome is ambiguous. Inspect live state and the audit log before taking another action."
                      : "Another worker claimed this approval. Reload after the apply finishes; if it remains here after a worker failure, reconcile it before retrying."}
                    {control?.last_error
                      ? ` Last error: ${control.last_error}`
                      : ""}
                  </p>
                ) : null}
                {needsReconciliation && !reconciliation ? (
                  <button
                    type="button"
                    className="reconcile-btn"
                    onClick={() => inspect(change.change_id)}
                  >
                    Inspect live state
                  </button>
                ) : null}
                {applying || reconciling ? (
                  <button
                    type="button"
                    className="reconcile-btn"
                    disabled={
                      !recoveryAvailable || resolving === change.change_id
                    }
                    onClick={() => recover(change)}
                  >
                    {recoveryAvailable
                      ? "Recover interrupted operation"
                      : change.recovery_available_at
                        ? `Recovery available ${new Date(
                            change.recovery_available_at,
                          ).toLocaleString()}`
                        : "Recovery time unavailable"}
                  </button>
                ) : null}
                {reconciliation ? (
                  <div className="reconciliation">
                    <strong>
                      Observed outcome: {reconciliation.assessment.outcome}
                    </strong>
                    {reconciliation.assessment.items.map((item, index) => (
                      <div className="item-line" key={index}>
                        <span>
                          {item.target} · {item.field}
                        </span>
                        <span>
                          {formatValue(item.observed)} ·{" "}
                          {item.state.replace(/_/g, " ")}
                        </span>
                      </div>
                    ))}
                    <details className="approval-history">
                      <summary>Approval history</summary>
                      {reconciliation.events.map((event, index) => (
                        <div
                          className="history-line"
                          key={event.event_id ?? index}
                        >
                          <span>{event.event.replace(/_/g, " ")}</span>
                          <span>
                            {event.operator} ·{" "}
                            {new Date(event.occurred_at).toLocaleString()}
                          </span>
                        </div>
                      ))}
                    </details>
                    <div className="reconcile-actions">
                      <button
                        type="button"
                        disabled={
                          resolving === change.change_id ||
                          reconciliation.assessment.outcome !== "applied"
                        }
                        onClick={() =>
                          resolve(
                            change.change_id,
                            reconciliation,
                            "confirmed_applied",
                          )
                        }
                      >
                        Confirm fully applied
                      </button>
                      <button
                        type="button"
                        disabled={resolving === change.change_id}
                        onClick={() =>
                          resolve(
                            change.change_id,
                            reconciliation,
                            "accepted_current_state",
                          )
                        }
                      >
                        Accept current state
                      </button>
                    </div>
                  </div>
                ) : null}
                {change.status === "applied" ? (
                  <Evidence entries={change.evidence} />
                ) : null}
                {errors[change.change_id] ? (
                  <p className="control-note danger" role="alert">
                    {errors[change.change_id]}
                  </p>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
