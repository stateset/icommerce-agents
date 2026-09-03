"use client";

import { useState } from "react";
import { checkout } from "../../lib/api";
import type { CartPayload, CheckoutResponse } from "../../lib/types";

/** Formats one amount the host already gave us -- a number or an exact decimal string
 * -- for display. Never sums or multiplies: every total on this panel comes from the
 * engine (`subtotal_exact`/`grand_total_exact`/`total_exact` on `/shopping/cart/add`, or
 * the rounded `subtotal`/`line_total` a `cart_update` chat event carries). */
function displayAmount(value: number | string | null | undefined, currency = "USD"): string | null {
  if (value === null || value === undefined) return null;
  const amount = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(amount)) return null;
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
}

export function CartPanel({ cart, busy }: { cart: CartPayload | null; busy: boolean }) {
  const [placing, setPlacing] = useState(false);
  const [result, setResult] = useState<
    { ok: true; orderNumber: string; sealed: boolean; receiptId: string | null } | { ok: false; message: string } | null
  >(null);

  const items = cart?.items ?? [];
  const count = items.reduce((sum, item) => sum + item.quantity, 0);
  const currency = cart?.currency ?? "USD";
  const subtotal = displayAmount(cart?.subtotal_exact ?? cart?.subtotal ?? null, currency);

  async function placeOrder() {
    setPlacing(true);
    setResult(null);
    const { status, body } = await checkout();
    setPlacing(false);
    if (status === 200 && body?.order_number) {
      setResult({
        ok: true,
        orderNumber: body.order_number,
        sealed: body.receipt?.sealed ?? false,
        receiptId: body.receipt?.receipt_id ?? null,
      });
      return;
    }
    const message =
      (body as { detail?: { error_message?: string } } | null)?.detail?.error_message ??
      "The order could not be placed. Check that the bag has items and try again.";
    setResult({ ok: false, message });
  }

  return (
    <aside className="cart-col">
      <div className="cart-header">
        <h2>Your bag</h2>
        <span className="cart-count">{count} item{count === 1 ? "" : "s"}</span>
      </div>
      {items.length === 0 ? (
        <div className="cart-empty">
          Nothing in your bag yet.
          <br />
          Ask the assistant for something, or add a product it shows you.
        </div>
      ) : (
        <div className="cart-items">
          {items.map((item) => {
            const lineTotal = displayAmount(item.total_exact ?? item.line_total ?? null, currency);
            return (
              <div className="cart-item" key={item.product_id}>
                <div>
                  <div className="title">{item.title}</div>
                  <div className="meta">Qty {item.quantity}</div>
                </div>
                <div>{lineTotal ?? "—"}</div>
              </div>
            );
          })}
        </div>
      )}
      <div className="cart-footer">
        {subtotal !== null ? (
          <div className="subtotal-row">
            <span>Subtotal</span>
            <span>{subtotal}</span>
          </div>
        ) : null}
        <button
          type="button"
          className="place-order-btn"
          disabled={items.length === 0 || placing || busy}
          onClick={placeOrder}
        >
          {placing ? "Placing order..." : "Place order"}
        </button>
        <p className="place-order-note">
          This is the only action that completes an order. The assistant can add items and
          show you the bag, but it never checks out for you.
        </p>
        {result ? (
          result.ok ? (
            <div className="order-result success">
              <span className="order-number">Order {result.orderNumber}</span>
              {result.sealed ? (
                <span>Sealed kernel receipt {result.receiptId ?? "(unavailable)"} -- the engine vouched for this transaction.</span>
              ) : (
                <span>Receipt recorded, unsealed -- the kernel did not vouch for this write.</span>
              )}
            </div>
          ) : (
            <div className="order-result error">{result.message}</div>
          )
        ) : null}
      </div>
    </aside>
  );
}
