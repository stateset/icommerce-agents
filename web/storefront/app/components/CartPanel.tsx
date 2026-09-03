"use client";

import { useState } from "react";
import { checkout } from "../../lib/api";
import type { CartPayload, CheckoutResponse } from "../../lib/types";

function money(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value);
}

export function CartPanel({ cart, busy }: { cart: CartPayload | null; busy: boolean }) {
  const [placing, setPlacing] = useState(false);
  const [result, setResult] = useState<
    { ok: true; orderNumber: string; sealed: boolean; receiptId: string | null } | { ok: false; message: string } | null
  >(null);

  const items = cart?.items ?? [];
  const count = items.reduce((sum, item) => sum + item.quantity, 0);
  const subtotal = items.reduce((sum, item) => sum + item.price * item.quantity, 0);
  const currency = cart?.currency ?? "USD";

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
          {items.map((item) => (
            <div className="cart-item" key={item.product_id}>
              <div>
                <div className="title">{item.title}</div>
                <div className="meta">Qty {item.quantity}</div>
              </div>
              <div>{money(item.price * item.quantity, currency)}</div>
            </div>
          ))}
        </div>
      )}
      <div className="cart-footer">
        <div className="subtotal-row">
          <span>Subtotal</span>
          <span>{money(subtotal, currency)}</span>
        </div>
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
