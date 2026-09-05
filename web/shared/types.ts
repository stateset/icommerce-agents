/** Types both web apps read from the same host routes. App-specific shapes stay in
 * each app's own `lib/types.ts`. */

/** `GET /capabilities` -- whether a model is configured for this deployment. Present
 * or absent only, never valid or invalid. */
export interface Capabilities {
  assistant: "available" | "unconfigured";
  stablecoin_checkout: "available" | "disabled";
  stablecoin_refunds: "available" | "deployment_integration_required" | "disabled";
  direct_checkout: "available" | "disabled";
}

/** The sealed kernel receipt a governed `checkout.commit` returns. */
export interface KernelReceipt {
  ok: boolean;
  sealed: boolean;
  receipt_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

/** `public_payment` on the host: the same shape whether the storefront reads its own
 * payment or the portal reads the reconciliation queue. */
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
  receipt?: KernelReceipt;
  detail?: unknown;
}
