import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CartPayload } from "../../lib/types";

const api = vi.hoisted(() => ({
  checkout: vi.fn(),
  fetchStablecoinPayment: vi.fn(),
  quoteStablecoin: vi.fn(),
  settleStablecoin: vi.fn(),
}));
const wallet = vi.hoisted(() => ({
  connectWallet: vi.fn(),
  signStablecoinPayment: vi.fn(),
}));
vi.mock("../../lib/api", () => api);
vi.mock("../../lib/stablecoin", () => wallet);

import { CartPanel } from "./CartPanel";

const PENDING_KEY = "icommerce.pendingStablecoinPayment";
const ACCOUNT = `0x${"ab".repeat(20)}`;

const cart: CartPayload = {
  currency: "USD",
  items: [
    // price * quantity is deliberately not the engine total: the panel must display
    // what the host gave it and never multiply.
    { product_id: "TENT-RIDGE-TAN", title: "Ridge tent", price: 219, quantity: 2, total_exact: "400.00" },
  ],
  subtotal_exact: "400.00",
  grand_total_exact: "400.00",
};

function fillShipping() {
  const fields: Record<string, string> = {
    "First name": "Rowan",
    "Last name": "Lee",
    Address: "1 Main St",
    City: "Portland",
    "State / province": "OR",
    "Postal code": "97201",
    "Country code": "us",
  };
  for (const [label, value] of Object.entries(fields)) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
}

beforeEach(() => {
  sessionStorage.clear();
  for (const fn of [...Object.values(api), ...Object.values(wallet)]) fn.mockReset();
  api.fetchStablecoinPayment.mockResolvedValue({ status: 404, body: null });
});
afterEach(cleanup);

describe("CartPanel", () => {
  it("displays the engine's exact totals as given, never a product of price and quantity", () => {
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable={false} directCheckoutAvailable sessionId="s1" />,
    );
    expect(screen.getAllByText("$400.00").length).toBeGreaterThan(0);
    expect(screen.queryByText("$438.00")).toBeNull();
  });

  it("places a demo order and shows the sealed receipt the kernel returned", async () => {
    api.checkout.mockResolvedValue({
      status: 200,
      body: { order_number: "ORD-1", receipt: { ok: true, sealed: true, receipt_id: "rcpt-7" } },
    });
    const onPlaced = vi.fn();
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable={false} directCheckoutAvailable sessionId="s1" onPlaced={onPlaced} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Place demo order" }));
    await waitFor(() => expect(screen.getByText("Order ORD-1")).toBeTruthy());
    expect(screen.getByText(/Sealed kernel receipt rcpt-7/)).toBeTruthy();
    expect(onPlaced).toHaveBeenCalled();
  });

  it("surfaces the engine's refusal message when the order is rejected", async () => {
    api.checkout.mockResolvedValue({
      status: 422,
      body: { detail: { error_code: "cart.empty", error_message: "the cart is empty" } },
    });
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable={false} directCheckoutAvailable sessionId="s1" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Place demo order" }));
    await waitFor(() => expect(screen.getByText("the cart is empty")).toBeTruthy());
  });

  it("quotes, signs, and settles a stablecoin payment, then clears the pending marker", async () => {
    wallet.connectWallet.mockResolvedValue(ACCOUNT);
    api.quoteStablecoin.mockResolvedValue({
      status: 402,
      body: { paymentId: "pay-1", quoteDigest: "sha256:abc", accepts: [], expiresAt: "", x402Version: 2, resource: { url: "" } },
    });
    wallet.signStablecoinPayment.mockResolvedValue("signed-header");
    api.settleStablecoin.mockResolvedValue({
      status: 200,
      body: {
        state: "completed",
        order_number: "ORD-2",
        transaction_hash: "0xtx",
        receipt: { ok: true, sealed: true, receipt_id: "rcpt-9" },
      },
    });
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable directCheckoutAvailable={false} sessionId="s1" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pay with USDC" }));
    const confirm = screen.getByRole("button", { name: "Review and sign USDC payment" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fillShipping();
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);

    await waitFor(() => expect(screen.getByText("Order ORD-2")).toBeTruthy());
    expect(api.quoteStablecoin).toHaveBeenCalledWith(expect.objectContaining({ country: "US" }), ACCOUNT);
    expect(api.settleStablecoin).toHaveBeenCalledWith("pay-1", "sha256:abc", "signed-header");
    expect(screen.getByText(/On-chain transaction 0xtx/)).toBeTruthy();
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull();
  });

  it("keeps the pending marker and warns not to pay again when settlement needs reconciliation", async () => {
    wallet.connectWallet.mockResolvedValue(ACCOUNT);
    api.quoteStablecoin.mockResolvedValue({
      status: 402,
      body: { paymentId: "pay-2", quoteDigest: "sha256:def", accepts: [], expiresAt: "", x402Version: 2, resource: { url: "" } },
    });
    wallet.signStablecoinPayment.mockResolvedValue("signed-header");
    api.settleStablecoin.mockResolvedValue({ status: 202, body: { state: "reconciliation_required" } });
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable directCheckoutAvailable={false} sessionId="s1" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Pay with USDC" }));
    fillShipping();
    fireEvent.click(screen.getByRole("button", { name: "Review and sign USDC payment" }));
    await waitFor(() => expect(screen.getByText(/Do not pay again/)).toBeTruthy());
    expect(JSON.parse(sessionStorage.getItem(PENDING_KEY) ?? "{}")).toEqual({
      paymentId: "pay-2",
      sessionId: "s1",
    });
  });

  it("recovers a pending payment on load and shows the completed order", async () => {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify({ paymentId: "pay-3", sessionId: "s1" }));
    api.fetchStablecoinPayment.mockResolvedValue({
      status: 200,
      body: { state: "completed", order_number: "ORD-3", transaction_hash: "0xrecovered" },
    });
    render(
      <CartPanel cart={cart} busy={false} stablecoinAvailable directCheckoutAvailable={false} sessionId="s1" />,
    );
    await waitFor(() => expect(screen.getByText("Order ORD-3")).toBeTruthy());
    expect(api.fetchStablecoinPayment).toHaveBeenCalledWith("pay-3", "s1");
    expect(sessionStorage.getItem(PENDING_KEY)).toBeNull();
  });
});
