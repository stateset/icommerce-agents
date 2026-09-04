import { AgentApi } from "web-shared";
import type {
  Capabilities,
  CartPayload,
  CheckoutResponse,
  OrdersPayload,
  ShippingAddress,
  StablecoinChallenge,
  StablecoinPayment,
} from "./types";

// Production defaults to the same-origin BFF, which reads an access token from an
// HttpOnly cookie. Set NEXT_PUBLIC_API_URL=http://localhost:8000 for the direct local
// demo path used by scripts and browser checks.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/commerce";

/** `AgentApi`'s prefix is the host's own route prefix -- `/shopping/session`,
 * `/shopping/chat`, `/shopping/cart/add`, `/shopping/checkout` all line up directly. */
export const api = new AgentApi(API_URL, "/shopping");

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

/** This session's own bag, as it stands -- a read, no write involved. */
export async function fetchCart(): Promise<CartPayload | null> {
  return api.get<CartPayload>("/cart");
}

/** This session's own order history, including whatever a keyless tour placed. */
export async function fetchOrders(): Promise<OrdersPayload | null> {
  return api.get<OrdersPayload>("/orders");
}

export async function addToCart(productId: string, quantity = 1): Promise<CartPayload | null> {
  return api.post<CartPayload>("/cart/add", { product_id: productId, quantity });
}

/** The direct demo route that completes an order -- reached by the UI, never by a tool. */
export async function checkout(): Promise<{ status: number; body: CheckoutResponse | null }> {
  try {
    const response = await fetch(`${api.base}/checkout`, {
      method: "POST",
      headers: api.headers(),
    });
    const body = (await response.json().catch(() => null)) as CheckoutResponse | null;
    return { status: response.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

export async function quoteStablecoin(
  shippingAddress: ShippingAddress,
  payerAddress: string,
): Promise<{ status: number; body: StablecoinChallenge | null }> {
  try {
    const response = await fetch(`${api.base}/checkout/stablecoin/quote`, {
      method: "POST",
      headers: api.headers(true),
      body: JSON.stringify({
        shipping_address: shippingAddress,
        payer_address: payerAddress,
      }),
    });
    const body = (await response.json().catch(() => null)) as StablecoinChallenge | null;
    return { status: response.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

export async function settleStablecoin(
  paymentId: string,
  quoteDigest: string,
  paymentSignature: string,
): Promise<{ status: number; body: StablecoinPayment | null }> {
  try {
    const response = await fetch(
      `${api.base}/checkout/stablecoin/${encodeURIComponent(paymentId)}`,
      {
        method: "POST",
        headers: { ...api.headers(true), "PAYMENT-SIGNATURE": paymentSignature },
        body: JSON.stringify({ quote_digest: quoteDigest }),
      },
    );
    const body = (await response.json().catch(() => null)) as StablecoinPayment | null;
    return { status: response.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

export async function fetchStablecoinPayment(
  paymentId: string,
  sessionId: string,
): Promise<{ status: number; body: StablecoinPayment | null }> {
  try {
    const response = await fetch(
      `${api.base}/payments/${encodeURIComponent(paymentId)}`,
      { headers: { ...api.headers(), "X-Session-Id": sessionId }, cache: "no-store" },
    );
    const body = (await response.json().catch(() => null)) as StablecoinPayment | null;
    return { status: response.status, body };
  } catch {
    return { status: 0, body: null };
  }
}
