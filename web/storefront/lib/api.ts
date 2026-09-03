import { AgentApi } from "web-shared";
import type { CartPayload, CheckoutResponse } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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

export async function addToCart(productId: string, quantity = 1): Promise<CartPayload | null> {
  return api.post<CartPayload>("/cart/add", { product_id: productId, quantity });
}

/** The only route that completes an order -- reached by this call alone, never by a tool. */
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
