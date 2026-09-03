"use client";

import { useState } from "react";
import { approveChange } from "../../lib/api";
import type { StagedChange } from "../../lib/types";
import { Evidence, parseEvidence } from "./Evidence";

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
            const evidence = parseEvidence(change.guardrail_notes ?? []);
            const approved = approvedIds.has(change.change_id);
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
                {change.status === "staged" ? (
                  <button
                    type="button"
                    className="approve-btn"
                    disabled={approving === change.change_id || approved}
                    onClick={() => approve(change.change_id)}
                  >
                    {approved
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
                {change.status === "applied" ? <Evidence entries={evidence} /> : null}
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}
