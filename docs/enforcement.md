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
| `checkout.commit` | `host/app.py`'s `POST /shopping/checkout` | **Yes** — a human click on the host route |
| `payments.create_refund` | `scripts/denials.py`, `tests/test_kernel.py` | No — script and test only |
| `payments.create` | `tests/test_kernel.py` | No — test only |
| `products.create` | nowhere | No — no code path issues it as a kernel command at all |

The bottom three are the honest part of this section. There is no host route and no
agent tool in this repo that issues a refund: `host/app.py` has no refund endpoint, and
no `MerchantBackend`/`StorefrontBackend` method calls `payments.create_refund`. Nothing
issues `payments.create` outside `tests/test_kernel.py`, which uses it as the simplest
governed command to assert a sealed receipt against. And nothing issues `products.create`
as a kernel command at all — `engine_backend/seed.py` creates the seeded catalog through
`commerce.products.create` on the binding, which is an ungoverned write like every other
binding call in this repo, and does not pass through the policy.

So two of the five grants are policy with no code path behind them, and a third is
exercised only by a script and a test. They are kept in `config/kernel-policy.json`
because the policy file is this deployment's declared subset of the engine's governed
set, and the point this document exists to make is precisely the gap between what the
engine governs and what a chat turn can reach — a policy trimmed to only the reachable
commands would state the smaller claim by hiding the larger one. Do not infer a refund
flow, a payment flow, or a product-creation flow from their presence in the policy or in
the denial demo: this repo shows those commands are governed, not that they are reachable.
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
| Checkout (complete an order) | no agent tool reaches this route — `POST /shopping/checkout` is a human-clicked host route, not a tool | `checkout.commit` | sealed kernel receipt (`receipt_id`) |
| Stage a price update | `check_listing_provenance`, `check_listing_record_read`, guardrail discount cap (`merchant_agent.changes.check_guardrails`) | none (staging only; no live write) | `StagedChange` record |
| Stage a listing/promotion/campaign/inventory action | same staging gates as above, per kind | none (staging only) | `StagedChange` record |
| Apply a price update | `check_apply_change` (provenance, guardrails, `APPROVAL_GATE` when `require_host_approval`) + `EngineMerchant.apply_change`'s own `approved_ids` check | none — direct SQL `UPDATE product_variants` | activity-log id |
| Apply an inventory action — restock, SKU has an inventory item | same as above | none — `commerce.inventory.adjust` (not in the governed set) | activity-log id |
| Apply an inventory action — restock, SKU has **no** inventory item | same as above | `inventory.item.create` | **sealed kernel receipt** |
| Apply an inventory action — pause/activate | same as above | none — direct SQL `UPDATE products SET status` | activity-log id |
| Apply a listing content update | same as above | none — `write_merchandising` custom object; direct SQL for `description` | activity-log id |
| Apply a promotion | same as above | none — direct SQL price update(s) + `promotion` custom object | activity-log id |
| Apply a campaign | same as above | none — `campaign` custom object | activity-log id |
| Refund | (issued only as a governed command — no tool in this repo issues a refund directly) | `payments.create_refund` (`requires_approval: true` in policy) | sealed kernel receipt, or an `agent-layer: blocked` outcome if attempted without approval evidence |

A "sealed kernel receipt" means `Receipt.sealed is True` and `Receipt.status ==
"succeeded"` — parsed from the engine's own JSON, not synthesized locally
(`engine_backend/kernel.py`). An "activity-log id" is an entry from
`commerce.activity_logs.record`, proving the write happened and who did it, but proving
nothing about whether the engine itself checked it — because for these writes, it did
not. A `blocked` outcome is `ToolOutcome.blocked` naming the gate that held the call;
the engine is never reached.

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

## The MCP path's approval is weaker than the HTTP host's

`docs/mcp.md` covers this in full; it is not restated here beyond the one sentence that
matters for this document's claim. The FastAPI host's `POST /merchant/changes/{id}/approve`
is a route only the operator's own browser session can reach — out-of-band by
construction, with the operator identity read from the session binding, never a request
body field. The MCP path's `host_approve` tool has no such separation: it is an ordinary
tool call, so its guarantee rests entirely on the connecting client prompting a human
before invoking it, and a client configured to auto-approve tool calls removes that
guarantee outright, with nothing in this process able to detect or refuse it. The two
paths are not equivalent, and the engine's own policy check on `payments.create_refund`
is what still holds regardless of which one an operator used to reach it.
