# stateset-icommerce-agents — design

A reference implementation that runs the `commerce-agents` agent architecture on the
`stateset-icommerce` embedded commerce engine.

## 1. Why this repo exists

Two reference implementations exist today and they do not overlap; they abut.

`commerce-agents` (Anthropic) is an **agent-layer** reference. Its substance is
model-facing discipline: third-party text is sanitized and fenced before the model reads
it, cart and staging writes accept only ids a tool returned in this session, presentation
payloads are re-joined from server records before rendering, merchant writes are staged
and apply only against a host approval mark, and one executor backs all three runtimes so
a rule written once holds on every path. What it does not have is commerce. Its two
backend interfaces are abstract, and all four verticals sit on in-memory dictionaries.
Nothing persists, nothing reconciles, and `checkout` charges nothing by design.

`stateset-icommerce` is an **engine-layer** reference and the exact complement. It has
everything the mocks fake: orders, carts, inventory, payments, returns, pricing,
analytics, a finance suite, exact decimal money, an auditable event log, a deny-overrides
policy kernel with explainable denials, and settlement paths. Its agent story is a tool
surface — 900+ MCP tools with `--apply` gating and a governed kernel. That is a strong
*enforcement* boundary and an absent *conversation* boundary: nothing there fences
third-party text, constrains which ids a model may write to, or turns a result into a
validated presentation.

This repo is the join. Neither upstream is forked or vendored.

## 2. Scope

In scope: both roles (shopping and merchant), one seeded vertical with both web surfaces,
two runtime paths (the Messages API host, and role MCP servers).

Out of scope for this spec: the Agent SDK consoles, Managed Agents manifests, ICP escrow
and x402 settlement, and any second vertical. Each is a later, separately specified
increment; none of them changes the interfaces below.

## 3. Repository layout

```
stateset-icommerce-agents/
├── vendor/commerce-agents/          # git submodule, pinned commit
├── engine_backend/                  # the deliverable: the adapter package
│   ├── store.py                     # EngineStore: Commerce handle, session→principal binding
│   ├── storefront.py                # StorefrontBackend over the engine
│   ├── merchant.py                  # MerchantBackend over the engine
│   ├── search.py                    # deterministic keyword + facet catalog search
│   ├── kernel.py                    # governed-command builder, receipt parsing
│   ├── staging.py                   # StagedChange persistence and apply dispatch
│   ├── content.py                   # policies and disclosures as store-owned objects
│   └── seed.py                      # the fictional demo store
├── host/                            # FastAPI: both roles, sessions, approval route
├── mcp/                             # one MCP server per role
├── web/storefront/  web/portal/     # Next.js apps
├── config/                          # kernel-policy.json, kernel-principal.json
├── docs/                            # enforcement.md, mapping.md, install.md
├── scripts/                         # install.sh, run_demo.py, denials.py, check.py
└── tests/
```

### Consuming the upstream packages

`commerce-agents` deliberately publishes nothing: its `requirements.txt` installs seven
packages editable from local directories, and each pyproject pins its siblings to a
version no index carries. So the upstream tree is a git submodule at
`vendor/commerce-agents`, pinned to a commit. This repo's `requirements.txt` installs the
seven packages editable from that path, in upstream's order, and pins its own third-party
dependencies to the same versions upstream tested against.

Skills, prompts, and tool descriptions are **read** from the pinned tree at run time
(`skills_dir=vendor/commerce-agents/shopping-agent/skills`). They are never copied. The
submodule commit is the version of the agent contract this repo claims to satisfy.

## 4. The adapter

`EngineStore` owns one `Commerce` handle over one SQLite file and binds a session to a
principal server-side. No tool argument ever names a customer, an operator, or a store;
identity comes from the session record, exactly as upstream requires.

`StorefrontBackend` (16 methods) and `MerchantBackend` (20 methods) are implemented in
full. The engine binding is synchronous and the interfaces are `async`, so every call runs
through `asyncio.to_thread`; a per-session lock serializes writes for one session, which
is what upstream's cart-write serialization requires anyway.

### Mapping

| Backend method | Engine surface |
|---|---|
| `search_products`, `get_product_details` | `products.list()`, `products.get()`, variants and merchandising attributes via `catalog.py` (below) |
| `get_cart`, `add_to_cart`, `update_cart_item`, `remove_from_cart` | `carts.get`, `add_item`, `update_item`, `remove_item` |
| `checkout_handoff` | returns a host URL; the storefront route calls `carts.complete()` |
| `get_orders`, `get_order` | `orders.*`, `shipments` for tracking |
| `get_preferences`, `get_account_context` | `customers`, `loyalty`, `segments` |
| `search_policies`, `get_disclosure` | `custom_objects` (store-owned documents) |
| `get_fulfillment_options` | `carts.get_shipping_rates()`, `shipping_zones` |
| `get_business_snapshot`, `query_metrics`, `get_campaign_performance` | `analytics.*` (14 methods) |
| `search_listings`, `get_listing` | `products`, `inventory`, `price_schedules` |
| `get_inventory_alerts` | `analytics.low_stock_items()`, `analytics.inventory_health()` |
| `get_order_issues` | `analytics.order_status_breakdown()`, `orders` |
| `get_pricing_context` | `price_schedules`, `price_levels`, unit cost from the product's merchandising object |
| `execute_analysis_query`, `get_analysis_schema` | a read-only SQLite connection on the same file |
| `stage_*`, `apply_change`, `discard_change`, `get_pending_changes` | see §5 |

### Type mapping

**Id convention.** A purchasable record's `product_id` is the engine **variant SKU**; a
family's `product_id` is the engine **product id**. The engine's `CartItem` exposes `sku`
but neither `product_id` nor `variant_id`, so keying purchasables by SKU is what makes
`update_cart_item` and `remove_from_cart` resolvable at all, and it keeps provenance ids
stable across turns. `get_product_details` accepts either shape.

Upstream's `Product` carries `options` and `option_values`; the engine's `ProductVariant`
supplies both, so a product with variants presents as a parent plus variant rows and
upstream's "held, pointed at its variants" gate works unmodified. Money is `float` on
upstream's models and exact decimal in the engine: conversion happens once, at the adapter
boundary, and every figure the model sees is derived from an engine value rather than
computed in the adapter.

### Four gaps, named rather than papered over

**Search.** The engine's semantic search (`commerce.vector(openai_api_key)`) requires an
OpenAI key, which is not acceptable in a Claude reference. `search.py` implements
deterministic keyword-plus-facet matching over the catalog, honoring
`SearchFilters` (category, price bounds, rating, attributes, sort). The README states the
substitution and what a deployment with its own search service replaces.

**Policies and disclosures.** The engine has no document domain. Both live in
`custom_objects` under a store-owned type. This still satisfies upstream's actual rule —
disclosure text is server-authored and the model only names a product it has seen.

**Campaigns.** The engine has no campaign domain. A staged campaign persists as a custom
object and applies to nothing live. The README says so rather than implying a marketing
stack.

**The catalog model is thinner than the agent's.** Two distinct shortfalls, handled two
distinct ways in `engine_backend/catalog.py`:

*Fields the engine does not store.* `Product` carries id, name, slug, description, and
status; `ProductVariant` adds sku, price, and compare-at price. Upstream's `Product` also
needs brand, category, image_url, rating, review_count, labels, attributes, and
option_values, and `get_pricing_context` needs unit cost. These live in one custom object
per product, type handle `merchandising`, `owner_type="product"`, written by `seed.py` and
read on every catalog call. They are store-owned data, not model-supplied.

*A read the binding does not expose.* `Commerce::get_variants(product_id)` exists in the
Rust crate but is not bound in Python 1.28.5, so a family's variants cannot be listed
through the binding at all. `catalog.py` reads them through the same read-only SQLite
connection `execute_analysis_query` uses, with a single parameterized SELECT.
`docs/mapping.md` lists every read taken this way — currently exactly one — so the
workaround stays visible and disappears when the binding grows the method.

## 5. Enforcement: two layers, and where the second one stops

`kernel/mutation-boundary.json` in the engine repo reports 938 tools, 474 mutations, and
**26 governed commands**. That asymmetry is the most useful fact in either repository, and
this reference is built around stating it precisely rather than flatteringly.

The governed 26 are the **transaction spine**, not the merchandising surface:
`checkout.commit`, `payments.create`, `payments.create_refund`, `returns.transition`,
`orders.transition`, `orders.ship`, `inventory.item.create`, `inventory.reserve`,
`inventory.reservation.confirm` / `.release`, `products.create`, `ledger.post`, and the
A2A escrow and x402 settlement commands.

**Governed — a sealed receipt comes back:**

- Checkout on the host route → `checkout.commit`.
- A refund or a return transition on the host route → `payments.create_refund`,
  `returns.transition`.
- A restock of a SKU with no inventory item yet → `inventory.item.create`.

**Adapter-guarded only — no governed command exists for these:**

- Every merchant `stage_*` write: `stage_listing_update`, `stage_price_update`,
  `stage_promotion`, `stage_campaign`, and the ordinary `stage_inventory_action`
  cases — a restock of an existing SKU is `inventory.adjust`, and a pause or activate is
  a product-status write. None of the three is a governed command.

These apply through direct binding writes under upstream's guardrails
(`check_guardrails`, `check_apply_change`) plus an `activity_logs` entry. They produce no
kernel receipt, and `docs/enforcement.md` says so in the same table that lists the ones
that do.

This is the finding, and it should be stated as one: **the engine governs the money and
the stock ledger; it does not govern merchandising.** A merchant agent editing listings
and prices is protected by its agent layer alone, which is exactly the case where
upstream's staged-change-plus-approval design is load-bearing rather than decorative.

### The doubled rule

`config/kernel-policy.json` sets `requires_approval: true` on `payments.create_refund`,
and the command envelope carries `ApprovalEvidence` (`approval_id`, `approved_by`,
`scope`, `approved_at`). Upstream's `require_host_approval` sets the same requirement at
the agent layer. They are the same rule enforced twice, and the kernel's copy holds even
when the agent layer is bypassed entirely — a caller reaching the engine directly still
cannot refund without evidence. `docs/enforcement.md` leads with this, because it is what
a two-layer reference is for.

### Demonstrated denials

`scripts/denials.py` runs three refusals end to end and prints the evidence each produces:

1. A cart write naming a product id the model never saw → agent gate, a `blocked` tool
   outcome naming the gate.
2. An `apply_change` without the host approval mark → agent gate, `blocked`.
3. A refund exceeding the captured amount, issued as a governed command → engine, inside
   the database transaction, a receipt whose `status` is failed and whose `error_code` is
   the engine's stable code, never parsed from prose.

The first two are agent-layer and prompt-independent. The third holds against any caller,
including one that never goes through this repo.

## 6. Staging

Upstream's `StagedChange` is a pydantic model the backend owns; the mock stores it in a
dict. Here it persists in `custom_objects` under a `staged_change` type, keyed by
`change_id`, so a staged change survives a host restart and is visible to anything reading
the engine.

`apply_change` dispatches on `ChangeKind`: `inventory_action` builds a governed kernel
command and records the receipt on the change; the other four kinds perform their binding
writes and record an activity-log id instead. Either way the change moves to `APPLIED`
with `applied_by` set to the operator principal from the session, never from a tool
argument.

`stage_*` and `apply_change` run upstream's guardrails unchanged — staged-time and
apply-time, against the config in force at apply time. The adapter adds no guardrails of
its own; it adds the engine layer beneath them.

## 7. Surfaces

**Messages API host.** One FastAPI app serving both roles on upstream's `X-Session-Id`
contract. A session binds a customer id or an operator principal server-side. The
storefront routes stream `text_delta`, `tool_call`, `ui`, `cart_update`, `turn_complete`;
the merchant routes stream `change_update` in place of `cart_update`. The portal's approve
route sets the mark that `apply_change` requires. The checkout route is where
`carts.complete()` is called, by a human click, never by the model.

**MCP servers.** One per role, built on `commerce_common/mcp_server.py`, loopback-bound
unless an environment variable states an authenticating gateway is in front. They expose
the *same* role tool list — roughly twenty disciplined tools, not nine hundred — over the
same executor and the same gates, so Claude Code drives the identical store under the
identical rules.

**Web.** Two Next.js apps in one npm workspace, built on the submodule's
`examples/web-shared` components, which the workspace resolves through a `file:` dependency
on `vendor/commerce-agents/examples/web-shared`. The single fallback, taken only if that
package does not build outside its own workspace, is two minimal single-page apps in this
repo with no shared component dependency; the implementation plan's first web step is to
build `web-shared` from this workspace and settle it.

## 8. Testing

- Each backend method against a seeded in-memory engine (`Commerce(":memory:")`), asserting
  the returned upstream types validate and the figures come from engine values.
- The three denials of §5, each asserting the specific evidence.
- One kernel-receipt assertion per governed command this repo issues.
- A fake-client turn test per role using `commerce_common/testing.py`, covering a search
  turn, a cart turn, and a stage-then-apply turn.
- `scripts/check.py` fails when the submodule commit moves without `docs/mapping.md`
  being re-verified, and asserts the backend classes implement every abstract method.
- `ruff check` and `ruff format --check` on a root config.

Nothing except the live smoke script needs an API key.

## 9. Constraints

Python 3.12 is the pin: `commerce-agents` requires ≥3.11 and `stateset-embedded` publishes
cp39–cp313 wheels, so 3.12 sits safely inside both. The published wheels are
`manylinux_2_34`; on a host with older glibc, pip falls back to the sdist and builds the
engine with cargo and maturin, which takes minutes rather than seconds. `docs/install.md`
states this.

The demo store is fictional throughout — an ACME-style catalog, invented brands, invented
figures — matching upstream's constraint and avoiding any claim about a real business.

## 10. Success criteria

1. Both backends implement every abstract method; no method raises `NotImplementedError`.
2. A conversation on each surface produces a persisted engine effect: a cart row, an
   order, an inventory movement, a staged change.
3. The three denials produce their three distinct kinds of evidence.
4. `docs/enforcement.md` states, for every write this repo can perform, which layer stops
   it and what evidence returns.
5. `ruff`, `pytest`, and `scripts/check.py` pass from a clean clone with one install
   command.
