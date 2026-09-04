"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchStablecoinPayments, reconcileStablecoinPayment } from "../../lib/api";
import type { StablecoinPayment } from "../../lib/types";

export function StablecoinPayments({ enabled, sessionId }: { enabled: boolean; sessionId: string | null }) {
  const [payments, setPayments] = useState<StablecoinPayment[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [transactions, setTransactions] = useState<Record<string, string>>({});
  const [working, setWorking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled || !sessionId) return;
    const next = await fetchStablecoinPayments();
    if (next) setPayments(next);
  }, [enabled, sessionId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function resolve(
    payment: StablecoinPayment,
    resolution: "confirmed_settled" | "confirmed_not_settled",
  ) {
    const note = notes[payment.payment_id]?.trim();
    if (!note || note.length < 8) {
      setError("Record at least eight characters describing the evidence you checked.");
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

  if (!enabled) return null;
  const unresolved = payments.filter((payment) => payment.state === "reconciliation_required");
  return (
    <section className="stablecoin-ops">
      <div className="stablecoin-ops-header">
        <h2>Stablecoin recovery</h2>
        <span>{unresolved.length} unresolved</span>
      </div>
      {error ? <p className="control-note danger" role="alert">{error}</p> : null}
      {unresolved.length === 0 ? (
        <p className="stablecoin-empty">No ambiguous settlements need attention.</p>
      ) : (
        unresolved.map((payment) => (
          <div className="stablecoin-payment" key={payment.payment_id}>
            <strong>{payment.amount} {payment.asset}</strong>
            <code>{payment.payment_id}</code>
            <span>{payment.network}</span>
            <input
              aria-label="Transaction hash"
              placeholder="0x transaction hash (if settled)"
              value={transactions[payment.payment_id] ?? payment.transaction_hash ?? ""}
              disabled={Boolean(payment.transaction_hash)}
              onChange={(event) =>
                setTransactions((current) => ({
                  ...current,
                  [payment.payment_id]: event.target.value,
                }))
              }
            />
            <textarea
              aria-label="Reconciliation evidence"
              placeholder="Provider and RPC evidence checked"
              maxLength={500}
              value={notes[payment.payment_id] ?? ""}
              onChange={(event) =>
                setNotes((current) => ({ ...current, [payment.payment_id]: event.target.value }))
              }
            />
            <div className="stablecoin-actions">
              <button
                type="button"
                disabled={working === payment.payment_id}
                onClick={() => resolve(payment, "confirmed_settled")}
              >
                Confirm settled
              </button>
              <button
                type="button"
                disabled={working === payment.payment_id || Boolean(payment.transaction_hash)}
                onClick={() => resolve(payment, "confirmed_not_settled")}
              >
                Confirm not settled
              </button>
            </div>
          </div>
        ))
      )}
    </section>
  );
}
