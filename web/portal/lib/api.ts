import { AgentApi } from "web-shared";
import {
  API_URL,
  UNREACHABLE,
  capabilities,
  controlRequest as sharedControlRequest,
  healthy,
} from "icommerce-shared";
import type { ControlResult } from "icommerce-shared";
import type {
  ReconciliationAssessment,
  ReconciliationDetail,
  StablecoinPayment,
  StablecoinRefund,
  StablecoinRefundPreview,
  StagedChange,
} from "./types";

export { API_URL, UNREACHABLE, capabilities, healthy };
export type { ControlResult };

/** `/merchant/session`, `/merchant/chat`, `/merchant/changes/{id}/approve` all line up
 * with the host's own routes under this prefix. */
export const api = new AgentApi(API_URL, "/merchant");

function controlRequest<T>(path: string, init: RequestInit): Promise<ControlResult<T>> {
  return sharedControlRequest<T>(`/merchant${path}`, init);
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

export async function fetchStablecoinPayments(): Promise<
  StablecoinPayment[] | null
> {
  const data = await api.get<{ payments: StablecoinPayment[] }>(
    "/stablecoin-payments",
  );
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

export async function previewStablecoinRefund(
  paymentId: string,
  amount: string,
): Promise<ControlResult<StablecoinRefundPreview>> {
  return controlRequest<StablecoinRefundPreview>(
    "/stablecoin-refunds/preview",
    {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({ payment_id: paymentId, amount }),
    },
  );
}

export async function applyStablecoinRefund(
  preview: StablecoinRefundPreview,
  idempotencyKey: string,
): Promise<ControlResult<StablecoinRefund>> {
  return controlRequest<StablecoinRefund>("/stablecoin-refunds", {
    method: "POST",
    headers: api.headers(true),
    body: JSON.stringify({
      payment_id: preview.payment_id,
      amount: preview.refund_amount,
      proposal_digest: preview.proposal_digest,
      idempotency_key: idempotencyKey,
    }),
  });
}

export async function fetchStablecoinRefunds(): Promise<
  StablecoinRefund[] | null
> {
  const data = await api.get<{ refunds: StablecoinRefund[] }>(
    "/stablecoin-refunds",
  );
  return data?.refunds ?? null;
}

export async function reconcileStablecoinRefund(
  refundId: string,
  resolution: "confirmed_refunded" | "confirmed_not_refunded",
  note: string,
  transactionHash?: string,
): Promise<ControlResult<StablecoinRefund>> {
  return controlRequest<StablecoinRefund>(
    `/stablecoin-refunds/${encodeURIComponent(refundId)}/reconcile`,
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
