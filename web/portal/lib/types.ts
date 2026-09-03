export interface ChangeItem {
  target: string;
  field: string;
  before?: unknown;
  after?: unknown;
}

export type ChangeStatus = "staged" | "applied" | "discarded";

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
  currency?: string | null;
}
