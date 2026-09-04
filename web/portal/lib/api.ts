import { AgentApi } from "web-shared";
import type {
  Capabilities,
  ReconciliationDetail,
  ReconciliationAssessment,
  StablecoinPayment,
  StagedChange,
} from "./types";

// Production defaults to the same-origin BFF, which reads an access token from an
// HttpOnly cookie. Set NEXT_PUBLIC_API_URL=http://localhost:8000 for the direct local
// demo path used by scripts and browser checks.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/commerce";

/** `/merchant/session`, `/merchant/chat`, `/merchant/changes/{id}/approve` all line up
 * with the host's own routes under this prefix. */
export const api = new AgentApi(API_URL, "/merchant");

export type ControlResult<T> = { ok: true; data: T } | { ok: false; error: string };

async function controlRequest<T>(path: string, init: RequestInit): Promise<ControlResult<T>> {
  try {
    const response = await fetch(`${API_URL}/merchant${path}`, init);
    const payload = (await response.json()) as T & { detail?: unknown };
    if (!response.ok) {
      const detail = payload.detail;
      return {
        ok: false,
        error: typeof detail === "string" ? detail : `Request failed (${response.status})`,
      };
    }
    return { ok: true, data: payload };
  } catch {
    return { ok: false, error: UNREACHABLE };
  }
}

export const UNREACHABLE =
  "Couldn't reach the ACME Supply API. Start it and try again.";

export async function healthy(): Promise<boolean> {
  try {
    const response = await fetch(`${API_URL}/healthz`, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

/** `GET /capabilities` -- not session-scoped, no header required. Distinguishes an
 * unconfigured deployment (no model) from an unreachable one (checked separately by
 * `healthy()`); never called unless the API already answered `healthy()`. */
export async function capabilities(): Promise<Capabilities | null> {
  try {
    const response = await fetch(`${API_URL}/capabilities`, { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as Capabilities;
  } catch {
    return null;
  }
}

/** Staged and applied changes with evidence and durable apply-control state -- a read,
 * no write involved. This is the artifact a keyless tour run leaves behind; fetched on
 * load so it is visible with no typing, whether or not an assistant is configured. */
export async function fetchChanges(): Promise<StagedChange[] | null> {
  const data = await api.get<{ changes: StagedChange[] }>("/changes");
  return data?.changes ?? null;
}

/** The only place approval happens; the operator comes from the session binding on the
 * host, never from this call's body. */
export async function approveChange(
  changeId: string,
  proposalDigest: string,
): Promise<ControlResult<{ change_id: string; approved_by: string }>> {
  return controlRequest<{ change_id: string; approved_by: string }>(
    `/changes/${encodeURIComponent(changeId)}/approve`,
    {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ proposal_digest: proposalDigest }),
    },
  );
}

export async function fetchReconciliation(
  changeId: string,
): Promise<ControlResult<ReconciliationDetail>> {
  return controlRequest<ReconciliationDetail>(
    `/changes/${encodeURIComponent(changeId)}/reconciliation`,
    { headers: api.headers() },
  );
}

export async function startReconciliation(
  changeId: string,
  proposalDigest: string,
): Promise<ControlResult<{ assessment: ReconciliationAssessment }>> {
  return controlRequest<{ assessment: ReconciliationAssessment }>(
    `/changes/${encodeURIComponent(changeId)}/reconciliation/start`,
    {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ proposal_digest: proposalDigest }),
    },
  );
}

export async function resolveReconciliation(
  changeId: string,
  proposalDigest: string,
  resolution: "confirmed_applied" | "accepted_current_state",
): Promise<ControlResult<{ assessment: ReconciliationAssessment }>> {
  return controlRequest<{ assessment: ReconciliationAssessment }>(
    `/changes/${encodeURIComponent(changeId)}/reconciliation`,
    {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ proposal_digest: proposalDigest, resolution }),
    },
  );
}

export async function fetchStablecoinPayments(): Promise<StablecoinPayment[] | null> {
  const data = await api.get<{ payments: StablecoinPayment[] }>("/stablecoin-payments");
  return data?.payments ?? null;
}

export async function reconcileStablecoinPayment(
  paymentId: string,
  resolution: "confirmed_settled" | "confirmed_not_settled",
  note: string,
  transactionHash?: string,
): Promise<ControlResult<StablecoinPayment>> {
  return controlRequest<StablecoinPayment>(
    `/stablecoin-payments/${encodeURIComponent(paymentId)}/reconcile`,
    {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({
        resolution,
        note,
        transaction_hash: transactionHash || null,
      }),
    },
  );
}
