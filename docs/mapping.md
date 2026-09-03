# Backend mapping

`engine_backend/` is the only place `commerce-agents`' two backend interfaces
(`StorefrontBackend`, `MerchantBackend`) meet the StateSet iCommerce engine
(`stateset-embedded`, installed as a pinned wheel, version `1.28.5`). This file records
what each read and write actually does, the one place the engine's Python binding is
read around with raw SQL, the write fallbacks Task 10 found, and the pinned vendor
commit `scripts/check.py` verifies against.

## Pinned vendor commit

`vendor/commerce-agents` is a git submodule, never edited in place. Its commit is
recorded here so `scripts/check.py` can catch a checkout that drifts from what this
document describes:

```
submodule-commit: fd4d59224ab96b43c6dc6888207c67b3bd5a24cf
```

## `EngineStorefront` (`engine_backend/storefront.py`)

The id convention: a purchasable record's `product_id` is the engine variant SKU; a
family's `product_id` is the engine product id. The engine's cart item carries only a
SKU, so SKU is the only key that makes `update_cart_item`/`remove_from_cart` resolvable
and keeps provenance ids stable across turns.

| `StorefrontBackend` method | Engine call |
|---|---|
| `search_products` | `engine_backend.search.search` over `commerce.products.list()`, filtered/sorted in Python; `category` is the one filter the engine understands natively |
| `get_product_details` | `commerce.products.get` / `get_variant_by_sku` + `catalog.read_merchandising` + `catalog.list_variants` |
| `get_cart` / `add_to_cart` / `update_cart_item` / `remove_from_cart` | `commerce.carts.*` (`for_customer`, `add_item`, `update_item`, `remove_item`) |
| `get_orders` / `get_order` | `commerce.orders.list` / `.get` |
| `get_preferences` | derived from `commerce.customers.get` (no preferences object in the engine) |
| `search_policies` / `get_disclosure` | `engine_backend/content.py`'s static ACME Supply policy text (the engine has no policy/disclosure domain) |
| `get_fulfillment_options` | fixed ACME Supply shipping options (the engine has no fulfillment-options domain) |
| `checkout_handoff` | renders the cart; charges nothing. Completing an order is `POST /shopping/checkout` on the host, which calls the governed `checkout.commit` kernel command directly — no agent tool reaches it. See `docs/enforcement.md`. |

## `EngineMerchant` (`engine_backend/merchant.py`)

| `MerchantBackend` method | Engine call |
|---|---|
| `get_business_snapshot` | `commerce.analytics.sales_summary` / `.customer_metrics` |
| `query_metrics` | `commerce.analytics.revenue_by_period` / `.top_products`; unsupported metrics and segments return an empty series with a `note` (see `task-9-report.md`) |
| `get_campaign_performance` | `campaign` custom objects (the engine has no campaign domain) |
| `search_listings` / `get_listing` | `catalog.catalog_rows` (category filtered via `engine_search`; the rest in Python) |
| `get_inventory_alerts` | `commerce.analytics.low_stock_items` (only `kind="low_stock"`; the engine has no slow-mover analytic) |
| `get_order_issues` | derived from `commerce.orders.list` (age vs. `fulfillment_status`) |
| `get_pricing_context` | `catalog.catalog_rows` + `Merchandising.unit_cost` |
| `execute_analysis_query` / `get_analysis_schema` | a capped, `SELECT`-only query straight through `store.readonly_sql()` (see below) — a deliberate feature, not a binding gap |
| `stage_*` / `get_pending_changes` / `discard_change` | `engine_backend/staging.py`'s `custom_objects`-backed `StagedChange` store; no live write |
| `apply_change` | dispatches by `ChangeKind` — see the table below |

### `apply_change` × engine write × governed? × evidence

One row per write `apply_change` can perform (from Task 10's report):

| `ChangeKind` | Engine write | Governed? | Evidence |
|---|---|---|---|
| `PRICE_UPDATE` | direct SQL `UPDATE product_variants SET price = ...` | No | activity-log entry |
| `INVENTORY_ACTION` (restock, SKU has an inventory item) | `commerce.inventory.adjust` | No | activity-log entry |
| `INVENTORY_ACTION` (restock, SKU has **no** inventory item) | kernel `inventory.item.create` | **Yes** | sealed receipt |
| `INVENTORY_ACTION` (pause/activate) | direct SQL `UPDATE products SET status = ...` | No | activity-log entry |
| `LISTING_UPDATE` | `write_merchandising` custom object; direct SQL for `description` | No | activity-log entry |
| `PROMOTION` | direct SQL price update(s) + `promotion` custom object | No | activity-log entry |
| `CAMPAIGN` | `campaign` custom object | No | activity-log entry |

Full detail, including the exact error codes and the schema/trigger inspection behind
the direct-SQL path, is in `docs/enforcement.md`.

## The one read-only SQL fallback

`stateset_embedded.Products` (the installed binding, version 1.28.5) exposes `create`,
`get`, `get_variant_by_sku`, `list`, and `count` — `Commerce::get_variants` exists in the
Rust crate but is **not bound in Python**. `engine_backend/catalog.py`'s `list_variants`
reads a product's variant SKUs with one parameterized `SELECT` on
`EngineStore.readonly_sql()` (a `mode=ro` SQLite connection, not the engine's own
handle) and resolves each SKU back through the bound `get_variant_by_sku`. This is the
only read this backend performs outside the Python binding.

`EngineMerchant.execute_analysis_query` also reads through `store.readonly_sql()`, but
it is not a fallback for a missing binding method — it is the deliberate `SELECT`-only,
row-capped analysis surface `get_analysis_schema` describes, gated by both a statement
heuristic and the connection's own `mode=ro` guard (see `tests/test_merchant_reads.py`).

`scripts/check.py` scans `engine_backend/*.py` for every function whose body reaches
`readonly_sql()` and fails if `docs/mapping.md` does not name it; both `list_variants`
and `execute_analysis_query` are named above for that reason.

## Direct-SQL write fallbacks

The binding exposes no mutator at all for three fields — no `products.update`, no
variant-price setter, no `products.update_status`. `EngineMerchant._write_sql` writes
them with a direct parameterized `UPDATE` on the store's own SQLite file, serialized
against other direct-SQL writes under one lock key (`"direct_sql"`), with
`PRAGMA busy_timeout` set so genuine contention waits rather than raising:

- `product_variants.price` (price updates, promotions)
- `products.status` (pause/activate)
- `products.description` (listing content)

This was verified against the schema and triggers directly, not assumed:
`PRAGMA table_info` on `products` and `product_variants` shows neither table has a
trigger that maintains `updated_at` or `version` — `_write_sql` sets both by hand, and
there is nothing else on either table for a raw `UPDATE` to miss. `products` does carry
`product_fts_{ai,ad,au}` triggers that keep its full-text index in sync from
`name`/`description`/`slug`; a trigger is schema-level, not connection-level, so it
fires the same way for a write from this module's own connection as it would for one
from the engine's own handle — a status/description write through this path keeps
`product_fts` consistent. `product_variants` has no trigger of any kind.

## The `web-shared` decision

`web/storefront` and `web/portal` (Task 13) import `AgentApi`, `useSession`,
`useAgentTurn`, and the `AgentEvent`/`ChatItem`/`UIBlock` types from
`vendor/commerce-agents/examples/web-shared` via an npm workspace
(`vendor/commerce-agents/examples/web-shared` listed alongside `web/*` in the root
`package.json`'s `workspaces`). The chat/turn state machine is upstream's code, not
reimplemented; presentation (product cards, the cart panel, the staged-changes panel)
is this repo's own, in plain CSS, and does not use `web-shared`'s Tailwind-v4-token
components (`ui.tsx`, `generative.tsx`, `storefront/bag.tsx`, `portal/*`) — pulling
those in would mean adopting Tailwind v4's custom-property utility syntax wholesale for
more surface than a from-scratch reference implementation needs.
