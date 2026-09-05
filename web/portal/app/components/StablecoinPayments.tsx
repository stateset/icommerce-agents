"use client";

import { useCallback, useEffect, useState } from "react";
import {
  applyStablecoinRefund,
  fetchStablecoinPayments,
  fetchStablecoinRefunds,
  previewStablecoinRefund,
  reconcileStablecoinPayment,
  reconcileStablecoinRefund,
} from "../../lib/api";
import type {
  StablecoinPayment,
  StablecoinRefund,
  StablecoinRefundPreview,
} from "../../lib/types";

export function StablecoinPayments({
  enabled,
  refundsEnabled,
  sessionId,
}: {
  enabled: boolean;
  refundsEnabled: boolean;
  sessionId: string | null;
}) {
  const [payments, setPayments] = useState<StablecoinPayment[]>([]);
  const [refunds, setRefunds] = useState<StablecoinRefund[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [transactions, setTransactions] = useState<Record<string, string>>({});
  const [working, setWorking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refundPaymentId, setRefundPaymentId] = useState("");
  const [refundAmount, setRefundAmount] = useState("");
  const [refundPreview, setRefundPreview] =
    useState<StablecoinRefundPreview | null>(null);
  const [refundKey, setRefundKey] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    if (enabled) {
      const next = await fetchStablecoinPayments();
      if (next) setPayments(next);
    }
    if (refundsEnabled) {
      const next = await fetchStablecoinRefunds();
      if (next) setRefunds(next);
    }
  }, [enabled, refundsEnabled, sessionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function resolvePayment(
    payment: StablecoinPayment,
    resolution: "confirmed_settled" | "confirmed_not_settled",
  ) {
    const note = notes[payment.payment_id]?.trim();
    if (!note || note.length < 8) {
      setError(
        "Record at least eight characters describing the evidence you checked.",
      );
      return;
    }
    setWorking(payment.payment_id);
    setError(null);
    try {
      const result = await reconcileStablecoinPayment(
        payment.payment_id,
        resolution,
        note,
        transactions[payment.payment_id]?.trim(),
      );
      if (!result.ok) setError(result.error);
      await refresh();
    } finally {
      setWorking(null);
    }
  }

  async function previewRefund() {
    setWorking("refund-preview");
    setError(null);
    try {
      const result = await previewStablecoinRefund(
        refundPaymentId.trim(),
        refundAmount.trim(),
      );
      if (result.ok) {
        setRefundPreview(result.data);
        setRefundKey(`stablecoin-refund:${crypto.randomUUID()}`);
      } else {
        setError(result.error);
      }
    } finally {
      setWorking(null);
    }
  }

  async function applyRefund() {
    if (!refundPreview || !refundKey) return;
    setWorking("refund-apply");
    setError(null);
    try {
      const result = await applyStablecoinRefund(refundPreview, refundKey);
      if (result.ok) {
        await refresh();
        if (result.data.state === "completed") {
          setRefundPreview(null);
          setRefundPaymentId("");
          setRefundAmount("");
          setRefundKey(null);
        } else {
          setError(
            "Refund outcome is ambiguous; reconcile it against treasury and RPC data.",
          );
        }
      } else {
        setError(result.error);
      }
    } finally {
      setWorking(null);
    }
  }

  async function resolveRefund(
    refund: StablecoinRefund,
    resolution: "confirmed_refunded" | "confirmed_not_refunded",
  ) {
    const note = notes[refund.refund_id]?.trim();
    if (!note || note.length < 8) {
      setError(
        "Record at least eight characters describing the evidence you checked.",
      );
      return;
    }
    setWorking(refund.refund_id);
    setError(null);
    try {
      const result = await reconcileStablecoinRefund(
        refund.refund_id,
        resolution,
        note,
        transactions[refund.refund_id]?.trim(),
      );
      if (!result.ok) setError(result.error);
      await refresh();
    } finally {
      setWorking(null);
    }
  }

  if (!enabled && !refundsEnabled) return null;
  const unresolvedPayments = payments.filter(
    (payment) => payment.state === "reconciliation_required",
  );
  const unresolvedRefunds = refunds.filter(
    (refund) => refund.state === "reconciliation_required",
  );
  return (
    <section className="stablecoin-ops">
      <div className="stablecoin-ops-header">
        <h2>Stablecoin operations</h2>
        <span>
          {unresolvedPayments.length + unresolvedRefunds.length} unresolved
        </span>
      </div>
      {error ? (
        <p className="control-note danger" role="alert">
          {error}
        </p>
      ) : null}

      {refundsEnabled ? (
        <div className="stablecoin-payment">
          <strong>Issue a refund</strong>
          <input
            aria-label="Stablecoin payment id"
            placeholder="pay_..."
            list="completed-stablecoin-payments"
            value={refundPaymentId}
            onChange={(event) => {
              setRefundPaymentId(event.target.value);
              setRefundPreview(null);
            }}
          />
          <datalist id="completed-stablecoin-payments">
            {payments
              .filter((payment) => payment.state === "completed")
              .map((payment) => (
                <option key={payment.payment_id} value={payment.payment_id}>
                  {payment.order_number ?? "Completed order"} — {payment.amount}{" "}
                  {payment.asset}
                </option>
              ))}
          </datalist>
          <input
            aria-label="Stablecoin refund amount"
            placeholder="Amount, for example 10.00"
            inputMode="decimal"
            value={refundAmount}
            onChange={(event) => {
              setRefundAmount(event.target.value);
              setRefundPreview(null);
            }}
          />
          {refundPreview ? (
            <p className="control-note">
              Return {refundPreview.refund_amount} {refundPreview.asset} on{" "}
              {refundPreview.network}? This action instructs the configured
              treasury service to move funds.
            </p>
          ) : null}
          <div className="stablecoin-actions">
            <button
              type="button"
              disabled={
                working !== null ||
                !refundPaymentId.trim() ||
                !refundAmount.trim()
              }
              onClick={() => void previewRefund()}
            >
              Review refund
            </button>
            <button
              type="button"
              disabled={working !== null || refundPreview === null}
              onClick={() => void applyRefund()}
            >
              Confirm on-chain refund
            </button>
          </div>
        </div>
      ) : null}

      {unresolvedPayments.map((payment) => (
        <div className="stablecoin-payment" key={payment.payment_id}>
          <strong>
            Payment: {payment.amount} {payment.asset}
          </strong>
          <code>{payment.payment_id}</code>
          <span>{payment.network}</span>
          <input
            aria-label="Payment transaction hash"
            placeholder="0x transaction hash (if settled)"
            value={
              transactions[payment.payment_id] ?? payment.transaction_hash ?? ""
            }
            disabled={Boolean(payment.transaction_hash)}
            onChange={(event) =>
              setTransactions((current) => ({
                ...current,
                [payment.payment_id]: event.target.value,
              }))
            }
          />
          <textarea
            aria-label="Payment reconciliation evidence"
            placeholder="Provider and RPC evidence checked"
            maxLength={500}
            value={notes[payment.payment_id] ?? ""}
            onChange={(event) =>
              setNotes((current) => ({
                ...current,
                [payment.payment_id]: event.target.value,
              }))
            }
          />
          <div className="stablecoin-actions">
            <button
              type="button"
              disabled={working === payment.payment_id}
              onClick={() => void resolvePayment(payment, "confirmed_settled")}
            >
              Confirm settled
            </button>
            <button
              type="button"
              disabled={
                working === payment.payment_id ||
                Boolean(payment.transaction_hash)
              }
              onClick={() =>
                void resolvePayment(payment, "confirmed_not_settled")
              }
            >
              Confirm not settled
            </button>
          </div>
        </div>
      ))}

      {unresolvedRefunds.map((refund) => (
        <div className="stablecoin-payment" key={refund.refund_id}>
          <strong>Refund: {refund.amount}</strong>
          <code>{refund.refund_id}</code>
          <span>Payment {refund.payment_id}</span>
          <input
            aria-label="Refund transaction hash"
            placeholder="0x transaction hash (if refunded)"
            value={
              transactions[refund.refund_id] ?? refund.transaction_hash ?? ""
            }
            onChange={(event) =>
              setTransactions((current) => ({
                ...current,
                [refund.refund_id]: event.target.value,
              }))
            }
          />
          <textarea
            aria-label="Refund reconciliation evidence"
            placeholder="Treasury and RPC evidence checked"
            maxLength={500}
            value={notes[refund.refund_id] ?? ""}
            onChange={(event) =>
              setNotes((current) => ({
                ...current,
                [refund.refund_id]: event.target.value,
              }))
            }
          />
          <div className="stablecoin-actions">
            <button
              type="button"
              disabled={working === refund.refund_id}
              onClick={() => void resolveRefund(refund, "confirmed_refunded")}
            >
              Confirm refunded
            </button>
            <button
              type="button"
              disabled={
                working === refund.refund_id || Boolean(refund.transaction_hash)
              }
              onClick={() =>
                void resolveRefund(refund, "confirmed_not_refunded")
              }
            >
              Confirm not refunded
            </button>
          </div>
        </div>
      ))}

      {unresolvedPayments.length === 0 && unresolvedRefunds.length === 0 ? (
        <p className="stablecoin-empty">
          No ambiguous transfers need attention.
        </p>
      ) : null}
    </section>
  );
}
