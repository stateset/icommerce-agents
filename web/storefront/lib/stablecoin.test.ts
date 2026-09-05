import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectWallet, signStablecoinPayment } from "./stablecoin";
import type { StablecoinChallenge } from "./types";

const ACCOUNT = `0x${"ab".repeat(20)}` as `0x${string}`;
const SIGNATURE = `0x${"cd".repeat(65)}`;

type Call = { method: string; params?: unknown[] };

/** A scripted EIP-1193 provider: records every request and answers from a table. */
function fakeWallet(answers: Partial<Record<string, unknown | ((call: Call) => unknown)>>) {
  const calls: Call[] = [];
  const wallet = {
    calls,
    async request(call: Call) {
      calls.push(call);
      const answer = answers[call.method];
      if (answer instanceof Error) throw answer;
      return typeof answer === "function" ? (answer as (c: Call) => unknown)(call) : answer;
    },
  };
  window.ethereum = wallet;
  return wallet;
}

function challenge(overrides: Partial<StablecoinChallenge> = {}): StablecoinChallenge {
  return {
    x402Version: 2,
    resource: { url: "https://shop.example/checkout" },
    accepts: [
      {
        scheme: "exact",
        network: "eip155:8453",
        amount: "21900000",
        asset: `0x${"11".repeat(20)}`,
        payTo: `0x${"22".repeat(20)}`,
        maxTimeoutSeconds: 300,
        extra: { name: "USD Coin", version: "2" },
      },
    ],
    paymentId: "pay-1",
    quoteDigest: `sha256:${"a".repeat(64)}`,
    expiresAt: new Date(Date.now() + 600_000).toISOString(),
    ...overrides,
  };
}

function decode(header: string): {
  accepted: { network: string };
  payload: { signature: string; authorization: Record<string, string> };
} {
  return JSON.parse(atob(header));
}

beforeEach(() => {
  vi.useFakeTimers({ now: new Date("2026-09-05T12:00:00Z") });
});
afterEach(() => {
  vi.useRealTimers();
  delete window.ethereum;
});

describe("connectWallet", () => {
  it("fails clearly when no wallet is present", async () => {
    await expect(connectWallet()).rejects.toThrow(/EVM wallet/);
  });

  it("returns the first account only when it is a well-formed address", async () => {
    fakeWallet({ eth_requestAccounts: [ACCOUNT] });
    expect(await connectWallet()).toBe(ACCOUNT);
    fakeWallet({ eth_requestAccounts: ["not-an-address"] });
    await expect(connectWallet()).rejects.toThrow(/valid EVM account/);
  });
});

describe("signStablecoinPayment", () => {
  it("signs an EIP-712 transfer authorization bound to the quote", async () => {
    const wallet = fakeWallet({ eth_chainId: "0x2105", eth_signTypedData_v4: SIGNATURE });
    const header = await signStablecoinPayment(challenge(), ACCOUNT);
    const signed = wallet.calls.find((c) => c.method === "eth_signTypedData_v4");
    const typed = JSON.parse((signed?.params as string[])[1]);
    expect(typed.domain).toEqual({
      name: "USD Coin",
      version: "2",
      chainId: 8453,
      verifyingContract: `0x${"11".repeat(20)}`,
    });
    expect(typed.primaryType).toBe("TransferWithAuthorization");
    expect(typed.message.from).toBe(ACCOUNT);
    expect(typed.message.to).toBe(`0x${"22".repeat(20)}`);
    expect(typed.message.value).toBe("21900000");
    expect(typed.message.nonce).toMatch(/^0x[0-9a-f]{64}$/);
    const body = decode(header);
    expect(body.payload.signature).toBe(SIGNATURE);
    expect(body.payload.authorization).toEqual(typed.message);
    expect(body.accepted.network).toBe("eip155:8453");
    // The wallet was already on the right chain, so no switch was requested.
    expect(wallet.calls.some((c) => c.method === "wallet_switchEthereumChain")).toBe(false);
  });

  it("asks the wallet to switch chains and never adds one itself", async () => {
    const wallet = fakeWallet({
      eth_chainId: "0x1",
      wallet_switchEthereumChain: null,
      eth_signTypedData_v4: SIGNATURE,
    });
    await signStablecoinPayment(challenge(), ACCOUNT);
    const switched = wallet.calls.find((c) => c.method === "wallet_switchEthereumChain");
    expect(switched?.params).toEqual([{ chainId: "0x2105" }]);
    expect(wallet.calls.some((c) => c.method === "wallet_addEthereumChain")).toBe(false);

    fakeWallet({ eth_chainId: "0x1", wallet_switchEthereumChain: new Error("user rejected") });
    await expect(signStablecoinPayment(challenge(), ACCOUNT)).rejects.toThrow(/Switch your wallet/);
  });

  it("caps validity at the quote expiry and refuses an expired quote", async () => {
    const wallet = fakeWallet({ eth_chainId: "0x2105", eth_signTypedData_v4: SIGNATURE });
    const soon = new Date(Date.now() + 60_000).toISOString();
    await signStablecoinPayment(challenge({ expiresAt: soon }), ACCOUNT);
    const typed = JSON.parse(
      (wallet.calls.find((c) => c.method === "eth_signTypedData_v4")?.params as string[])[1],
    );
    expect(Number(typed.message.validBefore)).toBe(Math.floor(new Date(soon).getTime() / 1000));

    const expired = new Date(Date.now() - 1_000).toISOString();
    await expect(signStablecoinPayment(challenge({ expiresAt: expired }), ACCOUNT)).rejects.toThrow(
      /expired/,
    );
  });

  it("rejects anything but an exact scheme and a well-formed signature", async () => {
    fakeWallet({ eth_chainId: "0x2105", eth_signTypedData_v4: "0xshort" });
    await expect(signStablecoinPayment(challenge(), ACCOUNT)).rejects.toThrow(/invalid payment signature/);
    const wrong = challenge();
    wrong.accepts = [{ ...wrong.accepts[0], scheme: "upto" as "exact" }];
    await expect(signStablecoinPayment(wrong, ACCOUNT)).rejects.toThrow(/exact-payment/);
  });
});
