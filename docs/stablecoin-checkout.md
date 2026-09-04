# Stablecoin checkout (x402 v2)

The host can accept an exact ERC-20 stablecoin payment through an x402 v2 facilitator
and then complete the quoted cart through StateSet's governed `checkout.commit` command.
The rail is disabled by default. No private key or seed phrase belongs in this service:
the payer signs client-side and the configured facilitator verifies and broadcasts.

This is an alternative checkout rail, not a claim that stablecoins remove the work of
running payments. Before mainnet, select a facilitator, confirm its supported network
and token contract, establish treasury/off-ramp operations, and obtain jurisdiction-
specific tax, sanctions, consumer-protection, refund, and accounting advice.

## Configuration

All values below are required when `ICOMMERCE_STABLECOIN_ENABLED=true`, except the
facilitator token. Addresses are deliberately not shipped with defaults: deployment
must pin the exact token contract and merchant recipient it reviewed.

```bash
export ICOMMERCE_STABLECOIN_ENABLED=true
export ICOMMERCE_X402_FACILITATOR_URL=https://facilitator.example.com
export ICOMMERCE_PUBLIC_BASE_URL=https://api.shop.example.com
export ICOMMERCE_STABLECOIN_NETWORK=eip155:8453
export ICOMMERCE_STABLECOIN_ASSET_SYMBOL=USDC
export ICOMMERCE_STABLECOIN_ASSET_NAME=USDC
export ICOMMERCE_STABLECOIN_ASSET_VERSION=2
export ICOMMERCE_STABLECOIN_ASSET_ADDRESS=0x0000000000000000000000000000000000000000
export ICOMMERCE_STABLECOIN_ASSET_DECIMALS=6
export ICOMMERCE_STABLECOIN_SETTLEMENT_CURRENCY=USD
export ICOMMERCE_STABLECOIN_MAX_AMOUNT=10000.00
export ICOMMERCE_STABLECOIN_PAY_TO=0x0000000000000000000000000000000000000000
export ICOMMERCE_STABLECOIN_QUOTE_TTL_SECONDS=300
export ICOMMERCE_STABLECOIN_PROCESSING_TIMEOUT_SECONDS=60
export ICOMMERCE_X402_FACILITATOR_TOKEN=optional-provider-credential
```

Replace both zero addresses; they only illustrate the required format. The host accepts
an EVM CAIP-2 network identifier and HTTPS origins, allows 30–900 second quotes, and
fails during startup when an enabled configuration is incomplete. Plain HTTP is allowed
for a loopback facilitator only; the shopper-facing public base URL is always HTTPS.
The processing timeout is configurable from 15–900 seconds and should remain longer
than the facilitator request timeout. After it elapses, abandoned pre-settlement work
returns safely to `quoted`, while abandoned settlement or order-commit work moves to
`reconciliation_required` because its external outcome cannot be inferred.

The reference adapter targets the interoperable x402 `exact` scheme. Confirm the
facilitator's `/verify` and `/settle` compatibility in testnet before enabling mainnet.
Do not point a production deployment at a public development facilitator.

## Protocol

Every route remains inside the normal shopping session and JWT subject binding.

1. Add items to the cart with `POST /shopping/cart/add`.
2. Call `POST /shopping/checkout/stablecoin/quote` with a shipping address and payer
   address. The host freezes the engine cart totals, item quantities, network, token,
   recipient, payer, address, and expiry into a SHA-256-bound quote.
3. The host answers `402` with an x402 v2 `PaymentRequired` body and its Base64 JSON in
   `PAYMENT-REQUIRED`. Additional top-level `paymentId`, `quoteDigest`, and `expiresAt`
   fields identify this immutable quote.
4. Sign the accepted requirement in an x402-compatible client/wallet and call
   `POST /shopping/checkout/stablecoin/{paymentId}` with the quote digest in JSON and
   the Base64 x402 payload in `PAYMENT-SIGNATURE`.
5. The host verifies the payload with the facilitator, checks the returned payer and
   network against the quote, settles it, durably records the transaction hash, and
   calls governed `checkout.commit` with a cart-derived idempotency key.
6. Success includes `PAYMENT-RESPONSE`, the order number, the settlement transaction,
   and the sealed StateSet kernel receipt. A client that loses this response can repeat
   the same request without another facilitator call or order.

`GET /shopping/payments/{paymentId}` returns the authenticated shopper's non-sensitive
status. It never returns wallet recipients, shipping data, payment payloads, or provider
credentials.

## Failure and recovery contract

`icommerce_stablecoin_payments` and its append-only event table are created beside the
engine tables. They provide cross-process claims, payload/transaction replay barriers,
and the recovery record for the interval between chain settlement and order creation.

| State | Meaning | Safe action |
|---|---|---|
| `quoted` | No valid payment has been accepted | Retry with a valid signature before expiry |
| `verifying` / `verified` | Verification is in progress | Wait; do not create another quote immediately |
| `settling` | A settlement call is in flight | Never blindly retry across a process failure |
| `settled` / `checkout_committing` | Funds moved; order completion is pending | Retry the same payment; `checkout.commit` is idempotent |
| `completed` | Settlement and governed order commit both succeeded | Return the recorded result |
| `failed` / `expired` | No settlement was accepted | Create a new quote after correcting the cart/configuration |
| `reconciliation_required` | Settlement may have moved funds, or checkout failed afterward | Compare facilitator and chain evidence before any new charge |

A timeout or malformed response from `/settle`, a negative settlement response, and a
settlement whose payer/network evidence differs from the quote all fail closed into
`reconciliation_required`. The automatic path will not call `/settle` again. Operators
can read the recovery queue at `GET /merchant/stablecoin-payments` and submit externally
verified chain truth to `POST /merchant/stablecoin-payments/{paymentId}/reconcile`. A
confirmed transaction resumes the idempotent order commit; confirmed non-settlement
makes the payment terminally failed. Each decision and operator note is kept in the
private audit event table, while shopper status exposes only a generic resolution.
The storefront stores only the non-secret payment id and original session id in
`sessionStorage`; after a refresh or lost response it polls the durable status record,
recovers the recorded receipt/order, and keeps an ambiguous payment visibly blocked
instead of presenting another pay button as the remedy. No signature, bearer token,
address, or facilitator payload is persisted in browser storage.

The route intentionally does not guess at chain truth. In JWT mode, reconciliation
also requires the dedicated `payments:reconcile` scope or `merchant_admin` role in
addition to normal merchant access. A production operator runbook
must compare facilitator and block-explorer/RPC evidence before using it to refund,
complete, or retry a payment.

The stablecoin journal is adapter-owned because `stateset-embedded` 1.28.5's Python
binding does not expose the engine's native x402 intent methods. StateSet remains the
cart/order authority and its policy kernel still commits checkout. When the Python
binding exposes native x402 intents, the adapter should dual-write/migrate these records
into that engine domain rather than silently claiming they are native engine payments.

## Deliberate boundaries

- The API is ready for autonomous x402 clients, and the Next.js storefront includes a
  provider-neutral EIP-1193 wallet flow: it requests an account, switches only to the
  configured chain, signs EIP-3009 typed data, and submits the x402 payload. It never
  adds an unknown chain or receives a private key. A deployment must still test the
  wallets and facilitator it chooses to support.
- Refunds still require a provider/chain-specific stablecoin refund workflow. The
  existing `payments.create_refund` route demonstrates engine-governed card-like refund
  policy, but it must not be presented as an on-chain stablecoin refund.
- Quote currency is the engine cart currency. A production deployment should only
  enable this 1:1 rail for a stablecoin/currency pair its treasury policy explicitly
  supports, or add an auditable FX oracle and quote-expiry policy.

Reference protocol: [x402 v2 specification](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md).
