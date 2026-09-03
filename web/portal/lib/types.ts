export interface ChangeItem {
  target: string;
  field: string;
  before?: unknown;
  after?: unknown;
}

export type ChangeStatus = "staged" | "applied" | "discarded";

/** The engine's two enforcement layers: a write the kernel governed and sealed a
 * receipt for, or one that only went through an ungoverned direct binding write and
 * left an activity-log entry. `engine_backend.apply` records this as structured
 * evidence at apply time, `engine_backend.staging` persists it keyed by `change_id`,
 * and the host attaches it verbatim (`host/app.py::_with_change_evidence`) -- nothing
 * parses `guardrail_notes` to recover it. */
export type EvidenceKind = "kernel_receipt" | "activity_log";

export interface ChangeEvidence {
  kind: EvidenceKind;
  id: string;
  note: string;
}

export interface StagedChange {
  change_id: string;
  kind: string;
  status: ChangeStatus;
  summary: string;
  items: ChangeItem[];
  created_at: string;
  created_by: string;
  applied_at?: string | null;
  applied_by?: string | null;
  guardrail_notes: string[];
  /** Structured evidence for applied changes; empty until the host attaches it. */
  evidence?: ChangeEvidence[];
  currency?: string | null;
}
