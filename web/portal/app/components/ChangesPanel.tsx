"use client";

import { useState } from "react";
import { approveChange } from "../../lib/api";
import type { StagedChange } from "../../lib/types";
import { Evidence } from "./Evidence";

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "--";
  return String(value);
}

export function ChangesPanel({ changes }: { changes: Record<string, StagedChange> }) {
  const [approving, setApproving] = useState<string | null>(null);
  const [approvedIds, setApprovedIds] = useState<Set<string>>(new Set());
  const list = Object.values(changes).sort((a, b) => a.created_at.localeCompare(b.created_at));

  async function approve(changeId: string) {
    setApproving(changeId);
    const result = await approveChange(changeId);
    setApproving(null);
    if (result) {
      setApprovedIds((prev) => new Set(prev).add(changeId));
    }
  }

  return (
    <aside className="changes-col">
      <div className="changes-header">
        <h2>Staged changes</h2>
        <p>Approve here, then ask the assistant to apply it.</p>
      </div>
      {list.length === 0 ? (
        <div className="changes-empty">
          Nothing staged yet. Ask the assistant to draft a price change, a restock, or a
          listing update and it will land here.
        </div>
      ) : (
        <div className="changes-list">
          {list.map((change) => {
            const control = change.apply_control;
            const controlState = control?.state;
            const approved =
              approvedIds.has(change.change_id) || controlState === "approved";
            const applying = controlState === "applying";
            const needsReconciliation = controlState === "reconciliation_required";
            const approvalBlocked = applying || needsReconciliation;
            return (
              <div className="change-card" key={change.change_id}>
                <div className="kind">{change.kind.replace(/_/g, " ")}</div>
                <div className="summary">{change.summary}</div>
                <div className="items">
                  {change.items.map((item, index) => (
                    <div className="item-line" key={index}>
                      <span>
                        {item.target} · {item.field}
                      </span>
                      <span>
                        {formatValue(item.before)} -&gt; {formatValue(item.after)}
                      </span>
                    </div>
                  ))}
                </div>
                <span className={`status-tag ${change.status}`}>{change.status}</span>
                {change.status === "staged" && controlState ? (
                  <span className={`status-tag control-${controlState}`}>
                    {controlState.replace(/_/g, " ")}
                  </span>
                ) : null}
                {change.status === "staged" ? (
                  <button
                    type="button"
                    className="approve-btn"
                    disabled={approving === change.change_id || approved || approvalBlocked}
                    onClick={() => approve(change.change_id)}
                  >
                    {applying
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
                    Ask the assistant to apply {change.change_id} to complete the write.
                  </p>
                ) : null}
                {change.status === "staged" && approvalBlocked ? (
                  <p className={`control-note ${needsReconciliation ? "danger" : ""}`}>
                    {needsReconciliation
                      ? "The write outcome is ambiguous. Inspect live state and the audit log before taking another action."
                      : "Another worker claimed this approval. Reload after the apply finishes; if it remains here after a worker failure, reconcile it before retrying."}
                    {control?.last_error ? ` Last error: ${control.last_error}` : ""}
                  </p>
                ) : null}
                {change.status === "applied" ? <Evidence entries={change.evidence} /> : null}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
