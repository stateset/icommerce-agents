# Backend mapping

`engine_backend/` is the only place `commerce-agents`' two backend interfaces
(`StorefrontBackend`, `MerchantBackend`) meet the StateSet iCommerce engine
(`stateset-embedded`, installed as a pinned wheel, version `1.28.5`). This file records
what each read and write actually does, the one place the engine's Python binding is
read around with raw SQL, the write fallbacks the binding forces, and the pinned vendor
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
| `search_products` | `engine_backend.search.search` over `commerce.products.list()`; every filter, `category` included, is applied in Python against this repo's own `Merchandising` custom object — the engine's catalog has no category column |
| `get_product_details` | `commerce.products.get` / `get_variant_by_sku` + `catalog.read_merchandising` + `catalog.list_variants` |
| `get_cart` / `add_to_cart` / `update_cart_item` / `remove_from_cart` | `commerce.carts.*` (`create` once per session, then `get_items`, `add_item`, `update_item`, `remove_item`). The session→cart mapping is `EngineStorefront._cart_ids`, read by the host through the host-only `session_cart_id`; `carts.for_customer` is not used by any backend method |
| `get_orders` / `get_order` | `commerce.orders.list` / `.get` |
| `get_preferences` | derived from `commerce.customers.get` (no preferences object in the engine) |
| `search_policies` / `get_disclosure` | `engine_backend/content.py`'s static ACME Supply policy text (the engine has no policy/disclosure domain) |
| `get_fulfillment_options` | `commerce.carts.get_shipping_rates` for the session's own cart; `product_ids` is unused (the engine has no per-item quote) and a session with no cart yet gets `[]` |
| `checkout_handoff` | renders the cart; charges nothing. Completing an order is `POST /shopping/checkout` on the host, which calls the governed `checkout.commit` kernel command directly — no agent tool reaches it. See `docs/enforcement.md`. |

## `EngineMerchant` (`engine_backend/merchant.py`)

| `MerchantBackend` method | Engine call |
|---|---|
| `get_business_snapshot` | `commerce.analytics.sales_summary` plus `get_inventory_alerts` for the low-stock count; traffic and conversion are `None` with a `note` (`customer_metrics` is not called) |
| `query_metrics` | `commerce.analytics.revenue_by_period` / `.top_products`; an unsupported metric or segment returns a series with no points and a `note` saying why, never a defaulted zero |
| `get_campaign_performance` | `campaign` custom objects (the engine has no campaign domain) |
| `search_listings` / `get_listing` | `catalog.catalog_rows`, with one catalog scan per call; every filter and sort is applied in Python |
| `get_inventory_alerts` | `commerce.analytics.low_stock_items` (only `kind="low_stock"`; the engine has no slow-mover analytic) |
| `get_order_issues` | derived from `commerce.orders.list` (age vs. `fulfillment_status`) |
| `get_pricing_context` | `catalog.catalog_rows` + `Merchandising.unit_cost` |
| `execute_analysis_query` / `get_analysis_schema` | a capped, `SELECT`-only query straight through `store.readonly_sql()` (see below) — a deliberate feature, not a binding gap |
| `stage_*` / `get_pending_changes` / `discard_change` | delegates to `engine_backend/staging.py`, which resolves the catalog rows, checks guardrails, and persists the result in its `custom_objects`-backed `StagedChange` store; no live write |
| `apply_change` | loads, validates status/approval, re-checks guardrails, then delegates the write to `engine_backend/apply.py`'s `apply_change(ctx, change)` — see the table below |

### `apply.apply_change` × engine write × governed? × evidence

One row per write `engine_backend/apply.py`'s `apply_change` can perform:

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

## Money at the seam

Every engine money figure is an exact decimal string (`product_variants.price` is a
`TEXT` column; every `*_exact` field is a string). `engine_backend/money.py` is the one
place that form changes: `exact(...)` quantizes to a two-place string (`ROUND_HALF_UP`)
and is what any write to an engine money column passes through, `to_float(...)` is the
one-way display conversion for the `float` price fields on the `commerce-agents` types,
and `discounted(...)` computes a promotion price in `Decimal` from the variant's own
exact string. No money arithmetic in this repo happens in binary floating point, and a
`PRICE_UPDATE`/`PROMOTION` `ChangeItem` carries its `before`/`after` as exact strings so
the figure that reaches `product_variants.price` is the one that was staged.

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
variant-price setter, no `products.update_status`. `EngineStore.write_sql` writes them
with a direct parameterized `UPDATE` on the store's own SQLite file, serialized against
other direct-SQL writes under one lock key (`"direct_sql"`), with `PRAGMA busy_timeout`
set so genuine contention waits rather than raising:

- `product_variants.price` (price updates, promotions) — always a two-place decimal
  string from `engine_backend/money.py`
- `products.status` (pause/activate)
- `products.description` (listing content)

`engine_backend/apply.py`'s dispatch functions call it directly (`ctx.store.write_sql`).
The write lives on the store, not on the merchant backend or its apply dispatch,
because of the invariant in the next section: a direct-SQL write is only sound in
combination with something the store owns, and keeping the two in one class is what
stops a second write path being added that forgets.

`scripts/check.py` scans `engine_backend/*.py` for every function containing
`sqlite3.connect(` and fails if this file does not name it — currently `write_sql`,
`_pin_connection`, and `readonly_sql` — so a new direct-SQL path cannot appear silently.

### Two SQLite libraries, one file: why the store pins a connection

This process has two independent SQLite libraries open on the same database: the
engine's own, bundled in the `stateset-embedded` Rust extension, and Python's `sqlite3`,
which `write_sql` and `readonly_sql` use for the writes and reads the binding does not
expose. On a WAL database they coordinate through the shared-memory WAL index, and that
coordination is stable only while the file stays continuously open. Let the last
connection close and the next one rebuilds the index from scratch while another
library's handle is still caching the old one.

The invariant is therefore: **the store keeps at least one connection open on the file
at all times.** `EngineStore._pin_connection` opens one read-only connection when the
store is constructed and never uses or closes it. This is not seeding-specific, not
table-scoped, and does not depend on what was written — any moment at which the file
drops to zero connections is enough, and a short-lived reader coming and going (a second
process, a backup, a debugging query) is the ordinary way that happens.

Three symptoms were observed with `stateset-embedded` 1.28.5 when nothing pinned the
file, all of them from the same cause:

- The engine's `Commerce` handle serving a pre-write price for the rest of its life
  while the row on disk is correct. In production terms this is the serious one: the
  host holds one `Commerce` per process and `catalog.list_variants` resolves through
  `get_variant_by_sku`, so the *second* applied price update or promotion of a process
  silently never reached the storefront — listings, cart lines and `subtotal_exact` all
  kept the stale price. That breaks the rule this repo exists to demonstrate, every
  money figure derives from an engine value, for exactly the writes that move money.
- A `mode=ro` connection returning a row from an entirely different table.
- `PRAGMA wal_checkpoint(TRUNCATE)` from another connection leaving the engine handle
  raising `disk I/O error`.

Measured over 60 write-then-read cycles with a short-lived reader on each cycle, the
engine handle is correct every time with the pinned connection held and stale on all but
the first without it, in both `wal` and `delete` journal modes. Reopening the `Commerce`
handle after each write was tried first and is not a substitute: it costs ~50ms per
write, it only works if the old handle is fully released *before* the new one is opened
(a new handle opened alongside the old one inherits the same stale view), and it does
not protect `readonly_sql`'s own reads.

`tests/test_store.py::test_a_direct_sql_write_is_visible_to_the_engine_handle` is the
regression test: it applies four successive direct-SQL price writes with an engine
binding write between them, exactly as an apply does, and asserts the engine handle
tracks every one. Disable the pin and it fails on the second write.

#### The pin is per process, and that is exactly what a second process needs

`_pin_connection` holds one connection per *process*, so a second host process on the
same store file — `scripts/run_demo.py` alongside a separately launched MCP server — is
not covered by the first process's connection. `tests/test_store_multiprocess.py`
measures what actually happens, and the answer is that **a second process is covered
precisely because it pins too.**

The staleness is not cross-process in origin. One process's direct-SQL connection churn
does not by itself poison another process's handle, and a writer process that opens,
writes and exits leaves a reader process's handle correct. But a second process's writes
are fully subject to the hazard. The moment an **unpinned** reader process makes a
transient `sqlite3` connection of its own — one opened and closed around a single
statement, which is exactly what `write_sql` does on every apply — its `Commerce` handle
stops seeing the other process's `write_sql` writes, permanently, while the row on disk
is correct. The open-and-close is what does it, not whether the statement reads or
writes; `readonly_sql()` is not an instance of it, since it caches one connection per
thread and holds it for that thread's life.

Two runtime knobs on one harness — the reader's pin closed or held, its transient disk
read included or omitted — over four successive price writes applied by a separate
process, deterministic over three repeats:

| reader's pin | reader does a transient read | what the reader's engine handle sees |
|---|---|---|
| held | yes | `199` OK, `189` OK, `179` OK, `169` OK |
| held | no | `199` OK, `189` OK, `179` OK, `169` OK |
| dropped | **yes** | `199` OK, then **stale on `199` for `189`, `179` and `169`** |
| dropped | no | `199` OK, `189` OK, `179` OK, `169` OK |

So the pin is neither redundant across processes nor merely a single-process concern: it
is the only thing standing between a two-process deployment and silently stale prices,
and every process that opens a store gets one. No fix is required beyond the pin already
being in the constructor, and there is no "one process per store file" constraint.

The bottom row of that table is a trap worth naming, because it is how the wrong
conclusion was reached here twice: **an incidental extra connection anywhere masks the
whole effect.** A harness whose reader performs an engine binding write before its read,
or leaves a diagnostic connection open beside the store, reads correct values with the
pin off and proves nothing. Any reproduction must open nothing except the store's own
connection and the transient read under test.

Three things about a second process are true and are *not* what the pin covers:

- The `"direct_sql"` lock is an `asyncio` lock, so it orders direct-SQL writes only
  within one process. Two processes writing at once are ordered by SQLite's own file
  lock and wait on the `busy_timeout` `write_sql` sets, not by that lock.
- `EngineMerchant._approved` is an in-memory set, and it is the gate `apply_change`
  checks before any write. Staged changes live in `custom_objects` and so are shared
  across processes, but the host approval that authorises applying one is not: an
  approval granted in one process does not authorise an apply in another, and does not
  survive a restart. This is the per-process item with the most riding on it, and the one
  a reader is most likely to assume is shared.
- The rest of the in-memory state beside the store is per process by construction:
  `EngineStore._bindings` and `EngineStorefront._cart_ids`. A session belongs to the
  process that opened it.

This was verified against the schema and triggers directly, not assumed:
`PRAGMA table_info` on `products` and `product_variants` shows neither table has a
trigger that maintains `updated_at` or `version` — the caller sets both by hand, and
there is nothing else on either table for a raw `UPDATE` to miss. `products` does carry
`product_fts_{ai,ad,au}` triggers that keep its full-text index in sync from
`name`/`description`/`slug`; a trigger is schema-level, not connection-level, so it
fires the same way for a write from this module's own connection as it would for one
from the engine's own handle — a status/description write through this path keeps
`product_fts` consistent. `product_variants` has no trigger of any kind.

## The `web-shared` decision

`web/storefront` and `web/portal` import `AgentApi`, `useSession`,
`useAgentTurn`, and the `AgentEvent`/`ChatItem`/`UIBlock` types from
`vendor/commerce-agents/examples/web-shared` via an npm workspace
(`vendor/commerce-agents/examples/web-shared` listed alongside `web/*` in the root
`package.json`'s `workspaces`). The chat/turn state machine is upstream's code, not
reimplemented; presentation (product cards, the cart panel, the staged-changes panel)
is this repo's own, in plain CSS, and does not use `web-shared`'s Tailwind-v4-token
components (`ui.tsx`, `generative.tsx`, `storefront/bag.tsx`, `portal/*`) — pulling
those in would mean adopting Tailwind v4's custom-property utility syntax wholesale for
more surface than a from-scratch reference implementation needs.
