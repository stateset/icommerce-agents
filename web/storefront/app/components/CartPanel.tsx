"use client";

import { useEffect, useState } from "react";
import {
  checkout,
  fetchStablecoinPayment,
  quoteStablecoin,
  settleStablecoin,
} from "../../lib/api";
import { connectWallet, signStablecoinPayment } from "../../lib/stablecoin";
import type { CartPayload, ShippingAddress } from "../../lib/types";

/** Formats one amount the host already gave us -- a number or an exact decimal string
 * -- for display. Never sums or multiplies: every total on this panel comes from the
 * engine (`subtotal_exact`/`grand_total_exact`/`total_exact` on `/shopping/cart/add`, or
 * the rounded `subtotal`/`line_total` a `cart_update` chat event carries). */
function displayAmount(value: number | string | null | undefined, currency = "USD"): string | null {
  if (value === null || value === undefined) return null;
  // The terminal display conversion: the engine's exact decimal string becomes a
  // JS number here, at the last step before formatting, and is never read back or
  // used in arithmetic.
  const amount = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(amount)) return null;
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(amount);
}

export function CartPanel({
  cart,
  busy,
  stablecoinAvailable,
  directCheckoutAvailable,
  sessionId,
  onPlaced,
}: {
  cart: CartPayload | null;
  busy: boolean;
  stablecoinAvailable: boolean;
  directCheckoutAvailable: boolean;
  sessionId: string | null;
  onPlaced?: () => void;
}) {
  const [placing, setPlacing] = useState<"direct" | "stablecoin" | null>(null);
  const [showStablecoin, setShowStablecoin] = useState(false);
  const [shipping, setShipping] = useState<ShippingAddress>({
    first_name: "",
    last_name: "",
    line1: "",
    city: "",
    state: "",
    postal_code: "",
    country: "US",
  });
  const [result, setResult] = useState<
    { ok: true; orderNumber: string; sealed: boolean; receiptId: string | null; transaction?: string | null }
    | { ok: false; message: string }
    | null
  >(null);

  const items = cart?.items ?? [];
  const count = items.reduce((sum, item) => sum + item.quantity, 0);
  const currency = cart?.currency ?? "USD";
  const subtotal = displayAmount(cart?.subtotal_exact ?? cart?.subtotal ?? null, currency);

  useEffect(() => {
    if (!stablecoinAvailable || !sessionId) return;
    const raw = sessionStorage.getItem("icommerce.pendingStablecoinPayment");
    if (!raw) return;
    let pending: { paymentId: string; sessionId: string };
    try {
      pending = JSON.parse(raw) as { paymentId: string; sessionId: string };
    } catch {
      sessionStorage.removeItem("icommerce.pendingStablecoinPayment");
      return;
    }
    if (!pending.paymentId || !pending.sessionId) return;

    let cancelled = false;
    async function recover() {
      const response = await fetchStablecoinPayment(pending.paymentId, pending.sessionId);
      if (cancelled || !response.body) return;
      const payment = response.body;
      if (payment.state === "completed" && payment.order_number) {
        window.clearInterval(timer);
        sessionStorage.removeItem("icommerce.pendingStablecoinPayment");
        setResult({
          ok: true,
          orderNumber: payment.order_number,
          sealed: payment.receipt?.sealed ?? false,
          receiptId: payment.receipt?.receipt_id ?? null,
          transaction: payment.transaction_hash,
        });
        onPlaced?.();
      } else if (payment.state === "failed" || payment.state === "expired") {
        window.clearInterval(timer);
        sessionStorage.removeItem("icommerce.pendingStablecoinPayment");
        setResult({ ok: false, message: `Payment ${pending.paymentId} did not settle.` });
      } else if (payment.state === "reconciliation_required") {
        setResult({
          ok: false,
          message: `Payment ${pending.paymentId} needs merchant reconciliation. Do not pay again.`,
        });
      }
    }
    const timer = window.setInterval(() => void recover(), 5000);
    void recover();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [onPlaced, sessionId, stablecoinAvailable]);

  async function placeOrder() {
    setPlacing("direct");
    setResult(null);
    const { status, body } = await checkout();
    setPlacing(null);
    if (status === 200 && body?.order_number) {
      setResult({
        ok: true,
        orderNumber: body.order_number,
        sealed: body.receipt?.sealed ?? false,
        receiptId: body.receipt?.receipt_id ?? null,
      });
      onPlaced?.();
      return;
    }
    const message =
      (body as { detail?: { error_message?: string } } | null)?.detail?.error_message ??
      "The order could not be placed. Check that the bag has items and try again.";
    setResult({ ok: false, message });
  }

  function errorMessage(body: unknown, fallback: string): string {
    if (!body || typeof body !== "object") return fallback;
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
      const message = (detail as { error_message?: unknown }).error_message;
      if (typeof message === "string") return message;
    }
    return fallback;
  }

  async function payWithStablecoin() {
    setPlacing("stablecoin");
    setResult(null);
    try {
      const account = await connectWallet();
      const quote = await quoteStablecoin(shipping, account);
      if (quote.status !== 402 || !quote.body?.paymentId) {
        throw new Error(errorMessage(quote.body, "The store could not create a payment quote."));
      }
      if (!sessionId) throw new Error("The shopping session is not ready.");
      sessionStorage.setItem(
        "icommerce.pendingStablecoinPayment",
        JSON.stringify({ paymentId: quote.body.paymentId, sessionId }),
      );
      const paymentSignature = await signStablecoinPayment(quote.body, account);
      const settled = await settleStablecoin(
        quote.body.paymentId,
        quote.body.quoteDigest,
        paymentSignature,
      );
      if (settled.status === 202) {
        throw new Error(
          `Payment ${quote.body.paymentId} needs merchant reconciliation. Do not pay again.`,
        );
      }
      if (settled.status !== 200 || settled.body?.state !== "completed" || !settled.body.order_number) {
        throw new Error(errorMessage(settled.body, "The payment could not be completed."));
      }
      setResult({
        ok: true,
        orderNumber: settled.body.order_number,
        sealed: settled.body.receipt?.sealed ?? false,
        receiptId: settled.body.receipt?.receipt_id ?? null,
        transaction: settled.body.transaction_hash,
      });
      sessionStorage.removeItem("icommerce.pendingStablecoinPayment");
      setShowStablecoin(false);
      onPlaced?.();
    } catch (error) {
      setResult({
        ok: false,
        message: error instanceof Error ? error.message : "The stablecoin payment failed.",
      });
    } finally {
      setPlacing(null);
    }
  }

  const shippingComplete = Boolean(
    shipping.first_name &&
      shipping.last_name &&
      shipping.line1 &&
      shipping.city &&
      shipping.state &&
      shipping.postal_code &&
      /^[A-Z]{2}$/.test(shipping.country),
  );

  return (
    <div className="cart-block">
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
        {stablecoinAvailable ? (
          <button
            type="button"
            className="place-order-btn"
            disabled={items.length === 0 || placing !== null || busy}
            onClick={() => setShowStablecoin((shown) => !shown)}
          >
            Pay with USDC
          </button>
        ) : null}
        {showStablecoin ? (
          <div className="stablecoin-form">
            <strong>Shipping address</strong>
            <div className="stablecoin-grid">
              {([
                ["first_name", "First name"],
                ["last_name", "Last name"],
                ["line1", "Address"],
                ["city", "City"],
                ["state", "State / province"],
                ["postal_code", "Postal code"],
                ["country", "Country code"],
              ] as const).map(([field, label]) => (
                <label key={field} className={field === "line1" ? "wide" : undefined}>
                  <span>{label}</span>
                  <input
                    value={shipping[field] ?? ""}
                    maxLength={field === "country" ? 2 : 200}
                    autoComplete={field.replace("_", "-")}
                    onChange={(event) =>
                      setShipping((current) => ({
                        ...current,
                        [field]: field === "country"
                          ? event.target.value.toUpperCase()
                          : event.target.value,
                      }))
                    }
                  />
                </label>
              ))}
            </div>
            <button
              type="button"
              className="stablecoin-confirm"
              disabled={!shippingComplete || placing !== null || busy}
              onClick={payWithStablecoin}
            >
              {placing === "stablecoin" ? "Confirm in wallet..." : "Review and sign USDC payment"}
            </button>
            <p>
              Your wallet signs an exact, short-lived authorization. The store never receives
              your private key and will not retry an uncertain settlement.
            </p>
          </div>
        ) : null}
        {directCheckoutAvailable ? (
          <button
            type="button"
            className="demo-order-btn"
            disabled={items.length === 0 || placing !== null || busy}
            onClick={placeOrder}
          >
            {placing === "direct" ? "Placing demo order..." : "Place demo order"}
          </button>
        ) : null}
        <p className="place-order-note">
          Only a trusted checkout action completes an order. The assistant can manage the bag,
          but it never signs or submits payment for you.
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
              {result.transaction ? <span>On-chain transaction {result.transaction}</span> : null}
            </div>
          ) : (
            <div className="order-result error">{result.message}</div>
          )
        ) : null}
      </div>
    </div>
  );
}
