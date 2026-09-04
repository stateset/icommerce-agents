# Enforcement

Upstream `commerce-agents`' `require_host_approval` and the StateSet iCommerce engine's
kernel policy's `requires_approval: true` are the same rule, enforced twice, by two
different layers that do not trust each other: the agent layer (`merchant_agent.gates`,
run before a tool's backend call) and the engine's own kernel (`Commerce.execute_kernel_command`,
checked against `config/kernel-policy.json` before any domain logic runs). The kernel's
copy holds even when the agent layer is bypassed entirely — a client that skips the
tool-call confirmation, a direct call into `engine_backend`, a bug in this repo's own
gates — because the engine checks its own policy on every governed command, not because
it trusts the caller to have checked first. `scripts/denials.py` demonstrates all three
failure points this buys: two refusals the agent layer alone produces, and one the
engine produces from inside its own transaction after the agent layer's approval check
has already passed.

## The finding: the engine governs the transaction spine, not merchandising

The engine governs 26 of its 474 mutations across its full command surface, and
those 26 are the transaction spine — checkout, payments, refunds, returns, order and
reservation transitions, and the inventory ledger. That is a fact about the engine,
independent of any one deployment of it.

Named in the engine's own mutation boundary and reproduced in this repo's design
record: `checkout.commit`, `payments.create`, `payments.create_refund`,
`returns.transition`, `orders.transition`, `orders.ship`, `inventory.item.create`,
`inventory.reserve`, `inventory.reservation.confirm`, `inventory.reservation.release`,
`products.create`, `ledger.post`, the composite checkout entry, and the A2A escrow and
x402 settlement commands. That enumeration is the transaction spine rather than a
complete roster — the remainder of the 26 are further commands of the same kinds — and
the point of it holds either way: not one governed command touches a listing's price,
status, description, imagery, promotion, or campaign.

This deployment enables five of those 26 in its own kernel policy
(`config/kernel-policy.json`): `inventory.item.create`, `checkout.commit`,
`payments.create`, `payments.create_refund`, `products.create`. That is a fact about
this repo, and it is a materially smaller claim than the engine's own figure — the
other 21 governed commands, including every return and reservation command, are not
wired into this policy at all. This repo also implements neither a return flow nor a
reservation flow anywhere in `engine_backend/`, so "the engine governs returns and
reservation transitions" is a true statement about the engine and a false one about
this deployment: those particular governed commands exist in the engine, are not in
this policy, and have no code path here that would reach them regardless.

Of the five commands this deployment does enable, only one of the seven
`MerchantBackend` write paths (`apply_change`'s `ChangeKind` dispatch) ever reaches one:
bringing a brand-new SKU into the stock ledger (`inventory.item.create`, when a restock
names a SKU with no existing inventory item). Every other merchant write — repricing,
merchandising content, promotions, campaigns, and even restocking a SKU the store
already tracks — is a direct binding write (or, for three fields, direct SQL) plus an
activity-log entry, with no kernel receipt at all. It does not govern merchandising.

### Which of the five are reachable, and from where

`KernelClient.execute` is called from exactly three places in this repo:
`engine_backend/apply.py`, `host/app.py`, and `scripts/denials.py`. Taking the five
enabled commands one at a time:

| Command | Issued from | Reachable in a running deployment? |
|---|---|---|
| `inventory.item.create` | `engine_backend/apply.py` (restock of a SKU with no inventory item) | **Yes** — an applied, approved staged change reaches it |
| `checkout.commit` | `host/app.py`'s demo-only direct checkout, or its x402 route after stablecoin settlement | **Yes** — explicit shopper action; no agent tool reaches either route, and JWT mode cannot create an unpaid direct order |
| `payments.create_refund` | `host/app.py`'s digest-bound operator refund routes; also `scripts/denials.py` and `tests/test_kernel.py` | **Yes** — an authenticated human operator, never an agent tool |
| `payments.create` | `tests/test_kernel.py` | No — test only |
| `products.create` | nowhere | No — no code path issues it as a kernel command at all |

The bottom two are the honest part of this section. No agent tool issues a refund, but
the host now provides a human-only preview/apply pair: preview canonicalizes the exact
payment and amount into a SHA-256 digest, and apply requires the authenticated operator
to echo that digest before `payments.create_refund` runs. Nothing issues
`payments.create` outside `tests/test_kernel.py`, which uses it as the simplest
governed command to assert a sealed receipt against. And nothing issues `products.create`
as a kernel command at all — `engine_backend/seed.py` creates the seeded catalog through
`commerce.products.create` on the binding, which is an ungoverned write like every other
binding call in this repo, and does not pass through the policy.

So one of the five grants has no code path behind it and another is exercised only by a
test. They are kept in `config/kernel-policy.json`
because the policy file is this deployment's declared subset of the engine's governed
set, and the point this document exists to make is precisely the gap between what the
engine governs and what a chat turn can reach — a policy trimmed to only the reachable
commands would state the smaller claim by hiding the larger one. Do not infer a general
payment or product-creation flow from their presence in the policy: the refund workflow
is deliberately narrower and remains outside Claude's capability surface.
`tests/test_kernel.py` fails if a command in the policy is not accounted for in this
table, so a grant added later cannot go undisclosed.

This means: **a merchant agent editing listings and prices is protected by its agent
layer alone.** There is no second, engine-side check on a price move, a status change,
or a promotion — the guardrails in `merchant_agent.changes` (the discount cap, the
provenance requirement, the approval gate) are the only thing standing between a model
and a listing write. This is exactly the case where upstream's staged-change-plus-
approval design is load-bearing rather than decorative: remove it, and nothing else in
this stack stops an unreviewed merchandising write.

## One row per write

| Write | Agent-layer gate | Engine-layer command | Evidence |
|---|---|---|---|
| Add to cart | `check_provenance`, `check_options` (`shopping_agent.gates`) | none | cart state only |
| Update/remove cart item | `check_provenance` (via `_check_provenance_or_cart`) | none | cart state only |
| Checkout (complete an order) | no agent tool reaches these routes — direct checkout is human-clicked and x402 checkout requires a payer signature plus facilitator settlement | `checkout.commit` | sealed kernel receipt (`receipt_id`); x402 also records the chain transaction |
| Stage a price update | `check_listing_provenance`, `check_listing_record_read`, guardrail discount cap (`merchant_agent.changes.check_guardrails`) | none (staging only; no live write) | `StagedChange` record |
| Stage a listing/promotion/campaign/inventory action | same staging gates as above, per kind | none (staging only) | `StagedChange` record |
| Apply a price update | `check_apply_change` (provenance, guardrails, `APPROVAL_GATE` when `require_host_approval`) + `EngineMerchant.apply_change`'s own `approved_ids` check | none — direct SQL `UPDATE product_variants` | activity-log id |
| Apply an inventory action — restock, SKU has an inventory item | same as above | none — `commerce.inventory.adjust` (not in the governed set) | activity-log id |
| Apply an inventory action — restock, SKU has **no** inventory item | same as above | `inventory.item.create` | **sealed kernel receipt** |
| Apply an inventory action — pause/activate | same as above | none — direct SQL `UPDATE products SET status` | activity-log id |
| Apply a listing content update | same as above | none — `write_merchandising` custom object; direct SQL for `description` | activity-log id |
| Apply a promotion | same as above | none — direct SQL price update(s) + `promotion` custom object | activity-log id |
| Apply a campaign | same as above | none — `campaign` custom object | activity-log id |
| Refund | authenticated human operator route; exact payment/amount preview bound to an echoed SHA-256 digest; no agent or MCP tool can issue it | `payments.create_refund` (`requires_approval: true` in policy) | sealed kernel receipt, including sealed transactional refusals such as `commerce.refund.exceeds_captured` |

A "sealed kernel receipt" means `Receipt.sealed is True` and `Receipt.status ==
"succeeded"` — parsed from the engine's own JSON, not synthesized locally
(`engine_backend/kernel.py`). An "activity-log id" is an entry from
`commerce.activity_logs.record`, proving the write happened and who did it, but proving
nothing about whether the engine itself checked it — because for these writes, it did
not. A `blocked` outcome is `ToolOutcome.blocked` naming the gate that held the call;
the engine is never reached.

Host-issued checkout and refund commands also carry the validated `X-Request-Id` as
their kernel-envelope `correlation_id`. That joins request logs to the engine command
without putting a session id, bearer token, customer id, or request body into logs.

## Two things the binding does not expose, and why the workaround is sound

The installed `stateset-embedded` binding (1.28.5) exposes no mutator for variant
price, product status, or product description — no `products.update`, no variant-price
setter. `engine_backend/apply.py`'s dispatch functions write these three fields with
direct parameterized SQL, through `EngineStore.write_sql`, against the store's own
SQLite file instead. This was checked against the schema and its triggers directly, not
assumed: `PRAGMA table_info` shows neither `products` nor `product_variants` has a
trigger that maintains `updated_at` or `version`, so setting both by hand (as those
writes do) is complete — there is nothing else on either table
for a raw `UPDATE` to miss. `products`' `product_fts_{ai,ad,au}` triggers, which keep its
full-text search index in sync, are schema-level rather than connection-level, so they
fire identically for a write from this module's own connection as for one from the
engine's own handle. See `docs/mapping.md` for the full write-fallback list.

Reaching the row correctly is only half of sound, and the other half is not free. Two
SQLite libraries are open on that file — the engine's and Python's — and a direct-SQL
write is visible to the engine's handle only because `EngineStore._pin_connection` holds
a share of the WAL index for the store's lifetime. Without that, the write lands on disk
and the handle serving the storefront never sees it, silently and for the rest of the
process's life; and the pin holds that share only because it reads a table, not merely
because it opens a connection. `docs/mapping.md` has the mechanism, the measurements, and
why the symptom appears on some Python SQLite builds and not others. Anything that
weakens the pin makes this workaround unsound again, whatever the schema says.

## One approval boundary for both runtime paths

The FastAPI host's `POST /merchant/changes/{id}/approve` is an out-of-band operator
route; the merchant MCP server exposes no approval tool. An MCP-staged proposal can be
approved only through that trusted host route (or an equivalent external integration)
against the shared database. A model therefore cannot satisfy its own approval gate,
even when its MCP client auto-approves every available tool call. `docs/mcp.md` covers
the deployment boundary and the historical finding that caused the old tool to be
removed.

The operator surface must echo the SHA-256 digest displayed with the reviewed diff; the
host rejects a mismatch. The backend then binds that digest and the approving operator in the durable
`icommerce_agent_approvals` SQLite ledger. Applying atomically claims an `approved`
record as `applying`, so one approval survives a restart and exactly one competing
process can own the attempt. A successful attempt becomes `applied`; a refusal before
mutation becomes `failed` and requires fresh approval.

If an exception happens after mutation dispatch begins, the ledger records
`reconciliation_required` and refuses both reapproval and another apply. A process crash
leaves the equally visible `applying` state. After the configured lease window (15
minutes by default), an operator can move that abandoned claim—not retry it—into
`reconciliation_required`; the transition is itself an audit event and retains every
target lease. This is deliberate: several engine calls
cannot be wrapped in the adapter ledger's SQLite transaction, so claiming that an
ambiguous multi-item attempt is safely retryable would risk duplicating or overwriting
part of it. `GET /merchant/changes` exposes this control state, and the portal renders
`approved`, `applying`, `reconciliation required`, and `resolved` from that durable
record rather than from browser-local memory.

Resolution is single-owner too. The ledger first moves
`reconciliation_required → reconciling`; only that operator may update the staged
lifecycle record and finish `reconciling → resolved`. A normal metadata failure returns
the claim to `reconciliation_required`. A resolver crash leaves `reconciling` visible
and recoverable after the same lease window, preventing two operators from recording
conflicting conclusions.

The approval claim transaction also inserts one durable lease per affected target.
Different change ids therefore cannot mutate the same SKU concurrently, even from
separate worker processes; a successful or safely refused attempt releases its leases,
while an ambiguous attempt retains them. Immediately before an overwrite, the adapter
also compares the current price, listing field, campaign field, or product status with
the `before` value the operator reviewed. If it moved after staging, the approval is
consumed and the change stays staged with an instruction to create and approve a fresh
diff. Additive restocks deliberately keep working across unrelated stock movement.

The HTTP route records the approval in two independent places: the session state that
Claude Commerce's executor checks before dispatch, and the operator-bound durable ledger
checked at the StateSet mutation boundary. After a merchant turn, the host reconciles
session marks with the ledger's remaining unspent marks, including when streaming or
the apply attempt fails.
