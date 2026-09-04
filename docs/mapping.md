# Backend mapping

`engine_backend/` is the only place `commerce-agents`' two backend interfaces
(`StorefrontBackend`, `MerchantBackend`) meet the StateSet iCommerce engine
(`stateset-embedded`, installed as a pinned wheel, version `1.28.5`). This file records
what each read and write actually does, the one place the engine's Python binding is
read around with raw SQL, the write fallbacks the binding forces, and the pinned vendor
commit `scripts/check.py` verifies against.

`engine_backend/stablecoins.py` is an adapter-owned x402 v2 boundary rather than a
`StorefrontBackend` method. It snapshots the session cart using exact engine totals,
creates a digest-bound quote, calls a configured facilitator's `/verify` and `/settle`
endpoints, and persists state through `StablecoinLedger.create`, `StablecoinLedger.get`,
and `StablecoinLedger.transition`. The payment tables are created by
`EngineStore._ensure_control_schema` before the embedded engine opens; later accesses
reuse `EngineStore._control_connection`, so the WAL pin described below protects these
short-lived Python SQLite connections too. The engine's Python binding at 1.28.5 does
not expose its native x402 intent APIs, so this journal is not represented as an engine
payment. Successful settlement is followed by the same governed `checkout.commit`
command as ordinary checkout. See `docs/stablecoin-checkout.md`.

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
| `get_cart` / `add_to_cart` / `update_cart_item` / `remove_from_cart` | `commerce.carts.*` (`create` once per session, then `get_items`, `add_item`, `update_item`, `remove_item`). First creation is serialized in process and atomically claimed in the durable `icommerce_agent_session_carts` table across processes, and the backend defensively enforces the configured per-item cap even when a direct host caller bypasses the executor. `EngineStorefront._cart_ids` is only a read cache; `carts.for_customer` is not used by any backend method |
| `get_orders` / `get_order` | `commerce.orders.list` / `.get` |
| `get_preferences` | derived from `commerce.customers.get` (no preferences object in the engine) |
| `search_policies` / `get_disclosure` | `engine_backend/content.py`'s static ACME Supply policy text (the engine has no policy/disclosure domain) |
| `get_fulfillment_options` | `commerce.carts.get_shipping_rates` for the session's own cart; `product_ids` is unused (the engine has no per-item quote) and a session with no cart yet gets `[]` |
| `checkout_handoff` | renders the cart; charges nothing. Completing an order uses a trusted host route: demo-only direct `POST /shopping/checkout`, or the disabled-by-default x402 stablecoin route after settlement. Both call the governed `checkout.commit` kernel command and no agent tool reaches either; JWT deployments cannot use the unpaid direct route. See `docs/enforcement.md`. `GET /shopping/cart` and `GET /shopping/orders` are session-scoped host reads over the same `get_cart`/`get_orders` methods, for the storefront web app to render live state with no model turn. |

## `EngineMerchant` (`engine_backend/merchant.py`)

| `MerchantBackend` method | Engine call |
|---|---|
| `get_business_snapshot` | `commerce.analytics.sales_summary` plus `get_inventory_alerts` for the low-stock count; traffic and conversion are `None` with a `note` (`customer_metrics` is not called) |
| `query_metrics` | `commerce.analytics.revenue_by_period` / `.top_products`; an unsupported metric or segment returns a series with no points and a `note` saying why, never a defaulted zero |
| `get_campaign_performance` | `campaign` custom objects (the engine has no campaign domain); updates merge omitted draft fields from the current record and refuse a stale reviewed field instead of replacing the whole object with `None` values |
| `search_listings` / `get_listing` | `catalog.catalog_rows`, with one catalog scan per call; every filter and sort is applied in Python |
| `get_inventory_alerts` | `commerce.analytics.low_stock_items` (only `kind="low_stock"`; the engine has no slow-mover analytic) |
| `get_order_issues` | derived from `commerce.orders.list` (age vs. `fulfillment_status`) |
| `get_pricing_context` | `catalog.catalog_rows` + `Merchandising.unit_cost` |
| `execute_analysis_query` / `get_analysis_schema` | delegates to `engine_backend/analysis.py`'s `run_query` and `SCHEMA`: a capped, `SELECT`-only query straight through `store.readonly_sql()` (see below) — a deliberate feature, not a binding gap |
| `stage_*` / `get_pending_changes` / `discard_change` | delegates to `engine_backend/staging.py`, which resolves the catalog rows, checks guardrails, and persists the result in its `custom_objects`-backed `StagedChange` store; no live write |
| `apply_change` | the HTTP approval route marks both Claude Commerce's per-session executor state and the adapter's durable, operator-bound approval ledger; then apply atomically claims that approval, loads and validates status, re-checks guardrails, locks the affected targets, refuses stale price/status previews, loads the change's staged payload, and delegates the write to `engine_backend/apply.py`'s `apply_change(ctx, change, payload)` — which returns the `APPLIED` copy of the change **and** the `Evidence` list the write produced. An ambiguous post-dispatch failure is held for reconciliation rather than made retryable. See the two tables below |

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

`payload` is the promotion or campaign draft the change was staged from
(`staging.load_change_payload`); every other kind ignores it.

Full detail, including the exact error codes and the schema/trigger inspection behind
the direct-SQL path, is in `docs/enforcement.md`.

## Deployment prompt policy (`engine_backend/agent_config.py`)

The host and live eval runner construct both roles from the same ACME-specific configs.
They retain the pinned upstream prompts and add explicit wording for the two failures
observed in the 2026-09-03 live eval: medical-condition shopping must name a qualified
clinician rather than substitute manufacturer documentation, and a successful
`stage_*` result must never be described as applied or live. The MCP servers repeat the
same rules in their server instructions because they expose tools rather than running
the Messages API agents themselves.

### `Evidence`: what actually backed a write

`apply.apply_change` returns a `staging.Evidence` per write alongside the applied
change, and `EngineMerchant.apply_change` persists it with the change record:

| Field | Meaning |
|---|---|
| `kind` | `"kernel_receipt"` for a governed command, `"activity_log"` for an ungoverned direct binding or direct-SQL write |
| `id` | the sealed receipt id, or the `commerce.activity_logs.record` entry id |
| `note` | the same human-readable line appended to the change's `guardrail_notes`, kept here so a reader of the structured field does not have to go looking for it |

It is set once, at apply time, from what actually happened — never inferred from
`guardrail_notes` prose, so a wording change cannot make evidence disappear.
`staging.load_evidence` reads it back, and `host/app.py` attaches it to the
`change_update` event the portal renders (`tests/test_host_evidence.py`). `GET
/merchant/changes` attaches the same stored evidence to a session-scoped list of staged
and applied changes, for the portal to render on load with no chat turn.

## The modules `engine_backend/` shares between the two roles

Three modules exist only so a mechanism is defined once rather than in each backend:

| Module | What it owns |
|---|---|
| `engine_backend/custom_objects.py` | The one shape this repo stores in the engine's custom objects: a type with a single required JSON `payload` field, one object per record. `ensure_payload_type`, `find_object`, `list_payloads`, `read_payload`, `write_payload`. Four things the engine has no domain for are kept this way — merchandising (`catalog.py`), policies and disclosures (`content.py`), staged changes (`staging.py`), and applied promotions and campaigns (`apply.py`) |
| `engine_backend/listings.py` | The family-then-variant resolution and shaping both roles do identically: `resolve_family_or_variant` returns a `FamilyResolution` or a `VariantResolution`, and `ListingShape` carries everything `storefront.py`'s `Product` and `merchant.py`'s `Listing` are each built from. The two record types are not collapsed — `Listing` has `stock`/`content_quality`/`status`, `Product` has `rating`/`review_count`/`in_stock` — only the lookup is |
| `engine_backend/analysis.py` | The merchant's read-only analysis surface: `SCHEMA` (the tables the agent is told about) and `run_query` (the single `SELECT`, capped at 100 rows and 8000 characters, on `store.readonly_sql()`). Kept out of `merchant.py` so a second entry point cannot keep the connection and drop the caps |
| `engine_backend/refunds.py` | The human operator refund preview: resolves a real payment, canonicalizes the exact amount, and binds store/payment/amount into the SHA-256 digest required by the governed host apply route. It performs no write itself |

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

`engine_backend/analysis.py`'s `run_query` — which `EngineMerchant.execute_analysis_query`
delegates to — also reads through `store.readonly_sql()`, but it is not a fallback for a
missing binding method: it is the deliberate `SELECT`-only, row-capped analysis surface
`get_analysis_schema` describes, gated by both a statement heuristic (`check_statement`)
and the connection's own `mode=ro` guard (see `tests/test_merchant_reads.py`).

`scripts/check.py` scans `engine_backend/*.py` for every function whose body reaches
`readonly_sql()` and fails if `docs/mapping.md` does not name it; `list_variants`,
`run_query` and `execute_analysis_query` are named above for that reason. It also fails
when a module in `engine_backend/` is named in neither this file nor `README.md`, which
is the same drift class one level up.

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
expose. On a WAL database they coordinate through the shared-memory WAL index — the
`-shm` file beside the `-wal`. When a connection closes and SQLite believes it is the
last user of that index, it checkpoints and **unlinks** both files. A handle that still
has the unlinked `-shm` mapped goes on reading it, now a private copy of a file nothing
will ever write to again, so every WAL frame appended afterwards is invisible to it for
the rest of its life while the row on disk is correct.

"Believes it is the last user" is where the two libraries part company, and it is the
whole of this hazard. That belief rests on a POSIX advisory lock, and POSIX advisory
locks are held per *process*: a lock taken by the engine's SQLite does not stop Python's
`sqlite3`, in the same process, from taking it exclusively. So `write_sql`'s connection
unlinked the index out from under a live `Commerce` handle on every close, and only a
connection belonging to *Python's* library could stop it. Between two processes the same
lock does work, which is why the cross-process results below come out the other way.

The invariant is therefore: **the store keeps one connection of Python's `sqlite3` open
on the file, and that connection must be a registered user of the WAL index — not merely
a descriptor on the file.** `EngineStore._pin_connection` opens one read-only connection
when the store is constructed, runs a single `SELECT count(*) FROM sqlite_schema` on it,
and never uses or closes it again. The read matters as much as the connection: it is what
maps the index. A pin that ran `SELECT 1` touched no table, opened no read transaction,
mapped nothing and prevented nothing; running the statement to completion (rather than
leaving a row unfetched) also releases the read transaction, so the pin does not block
checkpointing and the WAL does not grow without bound.

Three symptoms were observed with `stateset-embedded` 1.28.5 when nothing pinned the
index, all of them from the same cause:

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

#### Whether the symptom appears depends on Python's SQLite version

The engine's own SQLite is fixed: `stateset-embedded` 1.28.5 statically bundles SQLite
3.46.0, and the sdist and the `manylinux_2_34` wheel bundle the same source id, so a host
that compiles the engine and one that installs the wheel run identical engine-side code.
Python's `sqlite3` is not fixed, and that is the variable.

A `SELECT 1` pin — one that holds a descriptor but never joins the WAL index — was
measured against the four successive `write_sql` price writes the regression test
performs, on one machine, against one engine build:

| Python's `sqlite3` | with a `SELECT 1` pin | with the `sqlite_schema` pin |
|---|---|---|
| 3.45.1 | **stale from the second write on** | tracks every write |
| 3.46.0 | **stale from the second write on** | tracks every write |
| 3.47.1 | **stale from the second write on** | tracks every write |
| 3.50.4 | tracks every write | tracks every write |

So the weak pin was a real product defect, not a test artefact: on any host whose Python
links a SQLite older than the versions where this stops biting — most Linux
distributions — every merchandising write (price, product status, product description)
reached disk and never reached the engine handle serving the storefront. It survived
because this repo's own machine ships 3.50.4 and CI ships an older build, so only CI ever
saw it, and it was read as CI flakiness twice.

Two tests hold the guarantee, and they are complementary because of that table:
`tests/test_store.py::test_a_direct_sql_write_is_visible_to_the_engine_handle` applies
four successive direct-SQL price writes with an engine binding write between them,
exactly as an apply does, and asserts the engine handle tracks every one — the
behavioural half, whose sensitivity depends on the host's SQLite, and whose failure
message names that version. `test_the_pin_keeps_the_wal_index_from_being_unlinked`
asserts the physical half on any Linux host and any SQLite version: after a `write_sql`,
no descriptor in the process points at a deleted `-wal` or `-shm` and the `-shm` inode is
unchanged.

#### The pin is per process, and that is exactly what a second process needs

`_pin_connection` holds one connection per *process*, so a second host process on the
same store file — `scripts/run_demo.py` alongside a separately launched MCP server — is
not covered by the first process's connection. `tests/test_store_multiprocess.py`
measures what actually happens, and the answer is that **a second process is covered
precisely because it pins too.**

The staleness is not cross-process in origin, and the reason is the per-process scope of
POSIX advisory locks stated above. One process's direct-SQL connection churn does not by
itself poison another process's handle — the other process's own lock on the WAL index is
one this process really does have to respect — and a writer process that opens, writes
and exits leaves a reader process's handle correct. But a second process's writes are
fully subject to the hazard. The moment an **unpinned** reader process makes a
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
is the only thing standing between a deployment and silently stale prices, and every
process that opens a store gets one. That measurement was taken with a pin that only ran
`SELECT 1`, on a host whose Python links SQLite 3.50.4; read it as being about the pinned
connection existing, and read the version table above for what the connection has to
*do*. There is no "one process per store file" constraint.

The bottom row of that table is a trap worth naming, because it is how the wrong
conclusion was reached here twice: **an incidental extra connection anywhere masks the
whole effect.** A harness whose reader performs an engine binding write before its read,
or leaves a diagnostic connection open beside the store, reads correct values with the
pin off and proves nothing. Any reproduction must open nothing except the store's own
connection and the transient read under test.

Three things about a second process are true and are *not* what the pin covers:

The adapter's durable ledger is created by `EngineStore._ensure_control_schema` before
the embedded handle opens. Later ledger operations use the short-lived connection from
`EngineStore._control_connection`; the store's pinned connection keeps those transient
opens from invalidating the embedded handle's WAL view.

- The `"direct_sql"` lock is an `asyncio` lock, so it orders direct-SQL writes only
  within one process. Two processes writing at once are ordered by SQLite's own file
  lock and wait on the `busy_timeout` `write_sql` sets, not by that lock.
- Approval and its single-use claim live in the adapter-owned
  `icommerce_agent_approvals` table. They survive backend recreation, and a transactional
  `approved` → `applying` compare-and-set lets only one process claim a staged id. The
  same transaction claims every affected target in `icommerce_agent_target_leases`, so
  different changes for one SKU cannot cross-worker race either. Process death or an
  ambiguous post-dispatch failure deliberately retains the visible `applying` or
  `reconciliation_required` state and its target leases for operator reconciliation.
  A timeout-protected operator action transitions an abandoned `applying` claim into
  reconciliation without retrying it or releasing those leases.
  Reconciliation itself uses a second transactional claim (`reconciliation_required`
  → `reconciling` → `resolved`) so concurrent operators cannot persist conflicting
  lifecycle outcomes; an abandoned resolver is recoverable through the same timeout.
- Principal and session→cart mappings live in `icommerce_agent_sessions` and
  `icommerce_agent_session_carts`; `EngineStore._bindings` and
  `EngineStorefront._cart_ids` are caches. Conversation transcripts and upstream agent
  session state remain per process and require sticky routing for chat.

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
