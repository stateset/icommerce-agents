import { AgentApi } from "web-shared";
import { API_URL, UNREACHABLE, capabilities, healthy } from "icommerce-shared";
import type {
  CartPayload,
  CheckoutResponse,
  OrdersPayload,
  ShippingAddress,
  StablecoinChallenge,
  StablecoinPayment,
} from "./types";

export { API_URL, UNREACHABLE, capabilities, healthy };

/** `AgentApi`'s prefix is the host's own route prefix -- `/shopping/session`,
 * `/shopping/chat`, `/shopping/cart/add`, `/shopping/checkout` all line up directly. */
export const api = new AgentApi(API_URL, "/shopping");

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
