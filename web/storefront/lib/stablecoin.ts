import type { PaymentRequirement, StablecoinChallenge } from "./types";

interface EthereumProvider {
  request(args: { method: string; params?: unknown[] }): Promise<unknown>;
}

declare global {
  interface Window {
    ethereum?: EthereumProvider;
  }
}

function provider(): EthereumProvider {
  if (typeof window === "undefined" || !window.ethereum) {
    throw new Error("Install or open an EVM wallet to pay with stablecoin.");
  }
  return window.ethereum;
}

export async function connectWallet(): Promise<`0x${string}`> {
  const accounts = (await provider().request({ method: "eth_requestAccounts" })) as string[];
  const account = accounts?.[0];
  if (!account || !/^0x[0-9a-fA-F]{40}$/.test(account)) {
    throw new Error("The wallet did not return a valid EVM account.");
  }
  return account as `0x${string}`;
}

async function selectNetwork(requirement: PaymentRequirement): Promise<number> {
  const chainId = Number(requirement.network.slice("eip155:".length));
  if (!Number.isSafeInteger(chainId) || chainId <= 0) {
    throw new Error("The payment quote contains an unsupported network.");
  }
  const wanted = `0x${chainId.toString(16)}`;
  const current = (await provider().request({ method: "eth_chainId" })) as string;
  if (current.toLowerCase() !== wanted.toLowerCase()) {
    try {
      await provider().request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: wanted }],
      });
    } catch {
      throw new Error(
        `Switch your wallet to chain ${chainId}; this app never adds an unreviewed network.`,
      );
    }
  }
  return chainId;
}

function nonce(): `0x${string}` {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `0x${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function base64Json(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export async function signStablecoinPayment(
  challenge: StablecoinChallenge,
  account: `0x${string}`,
): Promise<string> {
  const accepted = challenge.accepts[0];
  if (!accepted || accepted.scheme !== "exact") {
    throw new Error("The server did not offer a supported exact-payment method.");
  }
  const chainId = await selectNetwork(accepted);
  const now = Math.floor(Date.now() / 1000);
  const quoteExpiry = Math.floor(new Date(challenge.expiresAt).getTime() / 1000);
  const validBefore = Math.min(now + accepted.maxTimeoutSeconds - 5, quoteExpiry);
  if (!Number.isFinite(quoteExpiry) || validBefore <= now) {
    throw new Error("The stablecoin quote expired; request a new quote.");
  }
  const authorization = {
    from: account,
    to: accepted.payTo,
    value: accepted.amount,
    validAfter: String(Math.max(0, now - 10)),
    validBefore: String(validBefore),
    nonce: nonce(),
  };
  const typedData = {
    domain: {
      name: accepted.extra?.name ?? "USDC",
      version: accepted.extra?.version ?? "2",
      chainId,
      verifyingContract: accepted.asset,
    },
    primaryType: "TransferWithAuthorization",
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
        { name: "verifyingContract", type: "address" },
      ],
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    message: authorization,
  };
  const signature = (await provider().request({
    method: "eth_signTypedData_v4",
    params: [account, JSON.stringify(typedData)],
  })) as string;
  if (!/^0x[0-9a-fA-F]{130}$/.test(signature)) {
    throw new Error("The wallet returned an invalid payment signature.");
  }
  const payload = {
    x402Version: 2,
    resource: challenge.resource,
    accepted,
    payload: { signature, authorization },
    extensions: challenge.extensions ?? {},
  };
  return base64Json(payload);
}
