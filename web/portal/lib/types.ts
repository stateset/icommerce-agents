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

export type ApplyControlState =
  | "approved"
  | "applying"
  | "applied"
  | "failed"
  | "reconciliation_required"
  | "reconciling"
  | "resolved";

export interface ApplyControl {
  change_id: string;
  approved_by: string;
  approved_at: string;
  state: ApplyControlState;
  attempt_id?: string | null;
  claimed_at?: string | null;
  finished_at?: string | null;
  last_error?: string | null;
  proposal_digest?: string | null;
  resolved_at?: string | null;
  resolved_by?: string | null;
  resolution?: string | null;
}

export interface ApprovalEvent {
  event_id?: number;
  change_id: string;
  event: string;
  operator: string;
  occurred_at: string;
  proposal_digest?: string | null;
  attempt_id?: string | null;
  detail?: string | null;
}

export interface ReconciliationItem {
  target: string;
  field: string;
  before?: unknown;
  intended_after?: unknown;
  observed?: unknown;
  state: "matches_before" | "matches_after" | "diverged" | "indeterminate";
}

export interface ReconciliationAssessment {
  change_id: string;
  outcome: "not_applied" | "applied" | "partial_or_diverged" | "indeterminate";
  items: ReconciliationItem[];
}

export interface ReconciliationDetail {
  change: StagedChange;
  proposal_digest: string;
  control: ApplyControl;
  assessment: ReconciliationAssessment;
  events: ApprovalEvent[];
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
  /** Durable backend approval/apply state; never inferred from browser state. */
  apply_control?: ApplyControl | null;
  proposal_digest?: string | null;
  approval_events?: ApprovalEvent[];
  recovery_available_at?: string | null;
  currency?: string | null;
}

export interface StablecoinPayment {
  payment_id: string;
  quote_digest: string;
  state: string;
  amount: string;
  currency: string;
  asset: string;
  network: string;
  expires_at: string;
  transaction_hash?: string | null;
  order_number?: string | null;
  last_error?: string | null;
}

/** `GET /capabilities` -- whether a model is configured for this deployment. Present
 * or absent only, never valid or invalid. */
export interface Capabilities {
  assistant: "available" | "unconfigured";
  stablecoin_checkout: "available" | "disabled";
  direct_checkout: "available" | "disabled";
}
