# stateset-icommerce-agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `StorefrontBackend` and `MerchantBackend` from `commerce-agents` over the `stateset-icommerce` embedded engine, with a FastAPI host, two MCP servers, two web apps, and documentation stating which enforcement layer stops which write.

**Architecture:** A pinned git submodule supplies the seven upstream Python packages, installed editable; `engine_backend/` is the only new library and holds one `Commerce` handle per store file. Reads that the Python binding does not expose fall back to a single read-only SQLite connection; merchandising fields the engine does not model live in store-owned custom objects. Governed writes go through `Commerce.execute_kernel_command`; everything else goes through the binding plus an activity-log entry.

**Tech Stack:** Python 3.12, `stateset-embedded==1.28.5`, the seven `commerce-agents` packages, FastAPI + uvicorn, `mcp`, pydantic v2, pytest + pytest-asyncio, ruff, Next.js 15 / TypeScript, npm workspaces.

**Spec:** `docs/superpowers/specs/2026-09-02-stateset-icommerce-agents-design.md`

## Global Constraints

- Python 3.12 exactly (`requires-python = ">=3.12,<3.13"`). Upstream needs ≥3.11; the engine ships cp39–cp313 wheels.
- The engine wheels are `manylinux_2_34`. On glibc < 2.34 pip builds the sdist with cargo and maturin — minutes, not seconds. Never work around this by pinning an older engine.
- `vendor/commerce-agents` is a **git submodule**, never edited. Skills and prompts are read from it at run time, never copied.
- No upstream file is modified. If a change to upstream seems required, stop and report it instead.
- Money: every figure the model sees derives from an engine value. Convert `*_exact` strings to float once, at the adapter boundary, with `float(Decimal(x))`. Never compute a price in the adapter.
- Identity is never a tool argument. Customer id, operator, store id, and principal come from the session record only.
- All demo data is fictional. The store is "ACME Supply"; no real brand, product, or person appears.
- A purchasable record's `product_id` / `listing_id` is the engine **variant SKU**; a family's is the engine **product id**.
- Every new Python file starts with no copyright header (this repo is MIT, upstream stays in `vendor/`).
- `ruff check .` and `ruff format --check .` must pass at every commit.

---

### Task 1: Repository scaffold, submodule, and a proving install

**Files:**
- Create: `.gitmodules` (via `git submodule add`), `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `ruff.toml`, `pytest.ini`, `.gitignore`, `engine_backend/__init__.py`, `scripts/install.sh`
- Test: `tests/test_install.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an importable `engine_backend` package; `UPSTREAM_ROOT: pathlib.Path` and `SKILLS_DIR(role: str) -> pathlib.Path` in `engine_backend/__init__.py`.

- [ ] **Step 1: Add the submodule and pin it**

```bash
cd /home/dom/stateset-icommerce-agents
git submodule add https://github.com/anthropics/commerce-agents.git vendor/commerce-agents
git -C vendor/commerce-agents checkout fd4d59224ab96b43c6dc6888207c67b3bd5a24cf
git add .gitmodules vendor/commerce-agents
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_install.py
from pathlib import Path

import pytest


def test_upstream_submodule_is_checked_out():
    from engine_backend import UPSTREAM_ROOT, SKILLS_DIR

    assert (UPSTREAM_ROOT / "commerce-common" / "commerce_common" / "fencing.py").is_file()
    assert SKILLS_DIR("shopping").is_dir()
    assert SKILLS_DIR("merchant").is_dir()
    with pytest.raises(ValueError):
        SKILLS_DIR("nope")


def test_both_worlds_import():
    import stateset_embedded  # the engine
    import commerce_common  # upstream shared layer
    from shopping_agent.backend import StorefrontBackend
    from merchant_agent.backend import MerchantBackend

    assert stateset_embedded.Commerce is not None
    assert commerce_common is not None
    assert StorefrontBackend is not None and MerchantBackend is not None


def test_engine_opens_in_memory():
    from stateset_embedded import Commerce

    commerce = Commerce(":memory:")
    assert commerce.products.count() == 0
```

- [ ] **Step 3: Run it and watch it fail**

Run: `python -m pytest tests/test_install.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine_backend'`.

- [ ] **Step 4: Write the packaging files**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "stateset-icommerce-agents"
version = "0.1.0"
description = "The commerce-agents architecture on the StateSet iCommerce engine."
requires-python = ">=3.12,<3.13"
license = { text = "MIT" }
dependencies = [
  "stateset-embedded==1.28.5",
  "fastapi>=0.141",
  "uvicorn>=0.52",
  "pydantic>=2.7",
]

[tool.hatch.build.targets.wheel]
packages = ["engine_backend", "host", "mcp_servers"]
```

```text
# requirements.txt — order matters: upstream pins siblings to versions no index carries
-e ./vendor/commerce-agents/commerce-common[examples]
-e ./vendor/commerce-agents/shopping-agent/core
-e ./vendor/commerce-agents/shopping-agent/runtime-messages-api
-e ./vendor/commerce-agents/merchant-agent/core
-e ./vendor/commerce-agents/merchant-agent/runtime-messages-api
-e .
```

```text
# requirements-dev.txt
-r requirements.txt
pytest>=8.0
pytest-asyncio>=0.23
ruff>=0.15,<0.17
httpx>=0.27
```

```toml
# ruff.toml
line-length = 100
target-version = "py312"

[lint]
select = ["E", "F", "I", "UP", "B"]

[lint.per-file-ignores]
"tests/*" = ["B011"]
```

```ini
; pytest.ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

```text
# .gitignore
.venv/
__pycache__/
*.db
*.db-shm
*.db-wal
node_modules/
.next/
.env
```

```bash
#!/usr/bin/env bash
# scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init --recursive
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
echo "Installed. Activate with: source .venv/bin/activate"
```

- [ ] **Step 5: Write the package module**

```python
# engine_backend/__init__.py
"""The commerce-agents backends implemented over the StateSet iCommerce engine."""

from pathlib import Path

UPSTREAM_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "commerce-agents"

_SKILL_DIRS = {
    "shopping": UPSTREAM_ROOT / "shopping-agent" / "skills",
    "merchant": UPSTREAM_ROOT / "merchant-agent" / "skills",
}


def SKILLS_DIR(role: str) -> Path:
    """The pinned upstream skills directory for a role. Read, never copied."""
    try:
        return _SKILL_DIRS[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(_SKILL_DIRS)}") from None


__all__ = ["UPSTREAM_ROOT", "SKILLS_DIR"]
```

- [ ] **Step 6: Install and run the tests**

Run: `chmod +x scripts/install.sh && ./scripts/install.sh && .venv/bin/python -m pytest tests/test_install.py -v`
Expected: PASS, all three tests. On glibc < 2.34 the install step spends several minutes compiling the engine — that is expected, not a hang.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Scaffold the repo and pin commerce-agents as a submodule"
```

---

### Task 2: EngineStore — one handle, session-bound identity

**Files:**
- Create: `engine_backend/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `engine_backend.UPSTREAM_ROOT`.
- Produces:
  - `class EngineStore` with `__init__(self, db_path: str, store_id: str = "store:acme")`,
  - `store.commerce -> stateset_embedded.Commerce`,
  - `async store.call(fn: Callable[[Commerce], T]) -> T` (runs `fn` on a worker thread),
  - `async store.write(session_key: str, fn: Callable[[Commerce], T]) -> T` (same, serialized per `session_key`),
  - `store.readonly_sql() -> sqlite3.Connection` (a cached read-only connection; raises `RuntimeError` for `:memory:`),
  - `class PrincipalBinding(BaseModel)` with `session_id: str`, `subject_id: str`, `kind: Literal["customer", "operator"]`, `store_id: str`,
  - `store.bind(session_id: str, subject_id: str, kind: str) -> PrincipalBinding` and `store.binding(session_id: str) -> PrincipalBinding` (raises `KeyError` when unbound).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import asyncio

import pytest

from engine_backend.store import EngineStore


@pytest.fixture
def store():
    return EngineStore(":memory:")


async def test_call_runs_on_a_worker_thread(store):
    count = await store.call(lambda c: c.products.count())
    assert count == 0


async def test_writes_for_one_session_are_serialized(store):
    order = []

    async def slow(tag):
        def body(_c):
            order.append(f"start-{tag}")
            order.append(f"end-{tag}")
            return tag

        return await store.write("s1", body)

    await asyncio.gather(slow("a"), slow("b"))
    assert order in (
        ["start-a", "end-a", "start-b", "end-b"],
        ["start-b", "end-b", "start-a", "end-a"],
    )


def test_binding_is_server_held(store):
    binding = store.bind("sess-1", "cust-9", "customer")
    assert binding.subject_id == "cust-9"
    assert binding.kind == "customer"
    assert store.binding("sess-1").subject_id == "cust-9"
    with pytest.raises(KeyError):
        store.binding("sess-unknown")


def test_readonly_sql_refuses_memory(store):
    with pytest.raises(RuntimeError):
        store.readonly_sql()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine_backend.store'`.

- [ ] **Step 3: Implement it**

```python
# engine_backend/store.py
"""One engine handle per store file, plus the server-held session→principal binding."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from typing import Literal, TypeVar

from pydantic import BaseModel
from stateset_embedded import Commerce

T = TypeVar("T")


class PrincipalBinding(BaseModel):
    """Who a session acts for. Set by the host at session start; never a tool argument."""

    session_id: str
    subject_id: str
    kind: Literal["customer", "operator"]
    store_id: str


class EngineStore:
    def __init__(self, db_path: str, store_id: str = "store:acme") -> None:
        self.db_path = db_path
        self.store_id = store_id
        self.commerce = Commerce(db_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._bindings: dict[str, PrincipalBinding] = {}
        self._sql: sqlite3.Connection | None = None

    async def call(self, fn: Callable[[Commerce], T]) -> T:
        return await asyncio.to_thread(fn, self.commerce)

    async def write(self, session_key: str, fn: Callable[[Commerce], T]) -> T:
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(fn, self.commerce)

    def readonly_sql(self) -> sqlite3.Connection:
        """A read-only connection for the reads the binding does not expose.

        Listed in docs/mapping.md; every use is a single parameterized SELECT.
        """
        if self.db_path == ":memory:":
            raise RuntimeError("a read-only connection needs a file-backed store, not :memory:")
        if self._sql is None:
            self._sql = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
            )
            self._sql.row_factory = sqlite3.Row
        return self._sql

    def bind(self, session_id: str, subject_id: str, kind: str) -> PrincipalBinding:
        binding = PrincipalBinding(
            session_id=session_id, subject_id=subject_id, kind=kind, store_id=self.store_id
        )
        self._bindings[session_id] = binding
        return binding

    def binding(self, session_id: str) -> PrincipalBinding:
        return self._bindings[session_id]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: PASS, four tests.

- [ ] **Step 5: Commit**

```bash
git add engine_backend/store.py tests/test_store.py
git commit -m "Add EngineStore: one engine handle, server-held session binding"
```

---

### Task 3: Merchandising objects, the seeded store, and catalog reads

**Files:**
- Create: `engine_backend/catalog.py`, `engine_backend/seed.py`
- Test: `tests/test_catalog.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `EngineStore`.
- Produces:
  - `MERCHANDISING_TYPE = "merchandising"` and `ensure_types(commerce) -> None` in `catalog.py`,
  - `class Merchandising(BaseModel)`: `brand: str | None`, `category: str | None`, `image_url: str | None`, `rating: float | None`, `review_count: int | None`, `labels: list[str]`, `attributes: dict[str, str]`, `option_names: list[str]`, `unit_cost: float | None`, `long_description: str | None`, `specs: dict[str, str]`, `variant_options: dict[str, dict[str, str]]` (SKU → option values),
  - `async read_merchandising(store, product_id) -> Merchandising` (defaults when absent),
  - `async write_merchandising(store, product_id, data: Merchandising) -> None`,
  - `async list_variants(store, product_id) -> list[stateset_embedded.ProductVariant]`,
  - `async catalog_rows(store) -> list[CatalogRow]` where `CatalogRow` is a dataclass with `product`, `variant`, `merch`, `stock: float`,
  - `seed_store(commerce) -> None` in `seed.py`.

- [ ] **Step 1: Write the shared fixtures**

```python
# tests/conftest.py
import pytest

from engine_backend.seed import seed_store
from engine_backend.store import EngineStore


@pytest.fixture
def store(tmp_path):
    """A file-backed seeded store — file-backed so readonly_sql() works."""
    engine_store = EngineStore(str(tmp_path / "store.db"))
    seed_store(engine_store.commerce)
    return engine_store
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_catalog.py
from engine_backend.catalog import (
    Merchandising,
    catalog_rows,
    list_variants,
    read_merchandising,
    write_merchandising,
)


async def test_seed_creates_a_catalog_with_variants(store):
    products = await store.call(lambda c: c.products.list())
    assert len(products) >= 6
    family = next(p for p in products if p.name == "Ridgeline 2-Person Tent")
    variants = await list_variants(store, family.id)
    assert {v.sku for v in variants} == {"TENT-RIDGE-GRN", "TENT-RIDGE-TAN"}


async def test_merchandising_round_trips(store):
    products = await store.call(lambda c: c.products.list())
    product = products[0]
    merch = await read_merchandising(store, product.id)
    assert merch.category is not None
    merch.labels = ["clearance"]
    await write_merchandising(store, product.id, merch)
    assert (await read_merchandising(store, product.id)).labels == ["clearance"]


async def test_merchandising_defaults_for_an_unknown_product(store):
    merch = await read_merchandising(store, "00000000-0000-0000-0000-000000000000")
    assert merch == Merchandising()


async def test_catalog_rows_carry_stock(store):
    rows = await catalog_rows(store)
    tent = next(r for r in rows if r.variant.sku == "TENT-RIDGE-GRN")
    assert tent.stock > 0
    assert tent.merch.category == "camping"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine_backend.catalog'`.

- [ ] **Step 4: Implement `catalog.py`**

```python
# engine_backend/catalog.py
"""What the engine's catalog does not model, and the one read its binding does not expose.

Merchandising fields (brand, category, imagery, ratings, option values, unit cost) live in
one custom object per product, owned by the product. Variants are read with a single
parameterized SELECT on the read-only connection, because Commerce::get_variants exists in
the Rust crate but is not bound in Python 1.28.5. docs/mapping.md lists that read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field
from stateset_embedded import Commerce, Product, ProductVariant

from engine_backend.store import EngineStore

MERCHANDISING_TYPE = "merchandising"


class Merchandising(BaseModel):
    brand: str | None = None
    category: str | None = None
    image_url: str | None = None
    rating: float | None = None
    review_count: int | None = None
    labels: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    option_names: list[str] = Field(default_factory=list)
    unit_cost: float | None = None
    long_description: str | None = None
    specs: dict[str, str] = Field(default_factory=dict)
    variant_options: dict[str, dict[str, str]] = Field(default_factory=dict)


def ensure_types(commerce: Commerce) -> None:
    """Create the custom object types this repo owns. Idempotent."""
    from stateset_embedded import CustomFieldDefinitionInput

    for handle, display, fields in (
        (
            MERCHANDISING_TYPE,
            "Merchandising",
            [CustomFieldDefinitionInput(key="payload", field_type="json", required=True)],
        ),
    ):
        if commerce.custom_objects.get_type_by_handle(handle) is None:
            commerce.custom_objects.create_type(
                handle=handle, display_name=display, fields=fields
            )


def _merch_object(commerce: Commerce, product_id: str):
    objects = commerce.custom_objects.list_objects(
        type_handle=MERCHANDISING_TYPE, owner_type="product", owner_id=product_id, limit=1
    )
    return objects[0] if objects else None


async def read_merchandising(store: EngineStore, product_id: str) -> Merchandising:
    record = await store.call(lambda c: _merch_object(c, product_id))
    if record is None:
        return Merchandising()
    return Merchandising.model_validate(json.loads(record.values_json)["payload"])


async def write_merchandising(store: EngineStore, product_id: str, data: Merchandising) -> None:
    values = json.dumps({"payload": data.model_dump()})

    def body(c: Commerce) -> None:
        ensure_types(c)
        record = _merch_object(c, product_id)
        if record is None:
            c.custom_objects.create_object(
                type_handle=MERCHANDISING_TYPE,
                values_json=values,
                owner_type="product",
                owner_id=product_id,
            )
        else:
            c.custom_objects.update_object(id=record.id, values_json=values)

    await store.write(f"merch:{product_id}", body)


async def list_variants(store: EngineStore, product_id: str) -> list[ProductVariant]:
    """The engine's variants for a product. See the module docstring for why this is SQL."""

    def body(_c: Commerce) -> list[ProductVariant]:
        cursor = store.readonly_sql().execute(
            "SELECT sku FROM product_variants WHERE product_id = ? ORDER BY sku", (product_id,)
        )
        skus = [row["sku"] for row in cursor.fetchall()]
        return [v for v in (store.commerce.products.get_variant_by_sku(s) for s in skus) if v]

    return await store.call(body)


@dataclass
class CatalogRow:
    product: Product
    variant: ProductVariant
    merch: Merchandising
    stock: float


async def catalog_rows(store: EngineStore) -> list[CatalogRow]:
    """Every purchasable variant with its product, merchandising, and stock."""
    products = await store.call(lambda c: c.products.list())
    rows: list[CatalogRow] = []
    for product in products:
        merch = await read_merchandising(store, product.id)
        for variant in await list_variants(store, product.id):
            stock = await store.call(lambda c, s=variant.sku: c.inventory.get_stock(s))
            rows.append(
                CatalogRow(
                    product=product,
                    variant=variant,
                    merch=merch,
                    stock=float(Decimal(str(stock.total_available))) if stock else 0.0,
                )
            )
    return rows
```

- [ ] **Step 5: Implement `seed.py`**

Write a fictional ACME Supply catalog: at least six products, one of which is the family `Ridgeline 2-Person Tent` with variants `TENT-RIDGE-GRN` and `TENT-RIDGE-TAN`. For each product create the engine product with its variants, an inventory item per SKU with an initial quantity, and one merchandising object. Also create two customers and three past orders so the shopping agent has order history, and complete a payment on one order so the refund denial in Task 7 has something to exceed.

```python
# engine_backend/seed.py
"""A fictional ACME Supply store. No real brand, product, or person appears."""

from __future__ import annotations

from stateset_embedded import (
    Commerce,
    CreateOrderItemInput,
    CreateProductVariantInput,
)

from engine_backend.catalog import Merchandising, ensure_types

_CATALOG = [
    {
        "name": "Ridgeline 2-Person Tent",
        "description": "A three-season backpacking tent for two.",
        "variants": [
            {"sku": "TENT-RIDGE-GRN", "name": "Green", "price": 219.00, "stock": 24},
            {"sku": "TENT-RIDGE-TAN", "name": "Tan", "price": 219.00, "stock": 4},
        ],
        "merch": {
            "brand": "ACME Outdoors",
            "category": "camping",
            "rating": 4.6,
            "review_count": 212,
            "unit_cost": 128.00,
            "option_names": ["colour"],
            "variant_options": {
                "TENT-RIDGE-GRN": {"colour": "green"},
                "TENT-RIDGE-TAN": {"colour": "tan"},
            },
            "attributes": {"season": "3-season", "capacity": "2"},
            "specs": {"packed_weight": "2.4 kg", "floor_area": "2.9 m2"},
            "long_description": "Aluminium poles, taped seams, two vestibules.",
        },
    },
    # Five more products follow the same shape: a sleeping bag (camping), a camp stove
    # (camping), a daypack (packs), a water filter (hydration), a headlamp (lighting).
    # Give at least one of them a stock of 0 so get_inventory_alerts has an alert, and
    # give one a rating below 4.0 so search sorting by rating is observable.
]


def seed_store(commerce: Commerce) -> None:
    """Idempotent: returns immediately when the catalog is already present."""
    ensure_types(commerce)
    if commerce.products.count() > 0:
        return

    from engine_backend.catalog import MERCHANDISING_TYPE
    import json

    for entry in _CATALOG:
        product = commerce.products.create(
            name=entry["name"],
            description=entry["description"],
            variants=[
                CreateProductVariantInput(sku=v["sku"], price=v["price"], name=v["name"])
                for v in entry["variants"]
            ],
        )
        commerce.custom_objects.create_object(
            type_handle=MERCHANDISING_TYPE,
            values_json=json.dumps({"payload": Merchandising(**entry["merch"]).model_dump()}),
            owner_type="product",
            owner_id=product.id,
        )
        for variant in entry["variants"]:
            commerce.inventory.create_item(
                sku=variant["sku"],
                name=f"{entry['name']} ({variant['name']})",
                initial_quantity=float(variant["stock"]),
                reorder_point=5.0,
            )

    customer = commerce.customers.create(
        email="rowan@example.invalid", first_name="Rowan", last_name="Ellis"
    )
    order = commerce.orders.create(
        customer_id=customer.id,
        items=[
            CreateOrderItemInput(
                sku="TENT-RIDGE-GRN", name="Ridgeline 2-Person Tent (Green)",
                quantity=1, unit_price=219.00,
            )
        ],
    )
    payment = commerce.payments.create(
        amount=219.00, currency="USD", order_id=order.id, customer_id=customer.id,
        payment_method="credit_card",
    )
    commerce.payments.complete(payment.id)
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_catalog.py -v`
Expected: PASS, four tests. If `list_variants` returns nothing, print the table names with `SELECT name FROM sqlite_master WHERE type='table'` on the read-only connection and correct the table or column name in the SELECT — the schema is the authority, not this plan.

- [ ] **Step 7: Commit**

```bash
git add engine_backend/catalog.py engine_backend/seed.py tests/
git commit -m "Add merchandising objects, variant reads, and the seeded ACME store"
```

---

### Task 4: Deterministic catalog search

**Files:**
- Create: `engine_backend/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `catalog_rows`, `CatalogRow`.
- Produces: `def score(row: CatalogRow, terms: list[str]) -> float` and `async search(store, query: str, filters: SearchFilters | None, limit: int) -> list[CatalogRow]`, returning one row per **family** (the lowest-priced in-stock variant represents it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_search.py
from shopping_agent.types import SearchFilters

from engine_backend.search import search


async def test_matches_on_title_and_respects_limit(store):
    rows = await search(store, "tent", None, limit=8)
    assert rows and rows[0].product.name == "Ridgeline 2-Person Tent"
    assert len(await search(store, "tent", None, limit=1)) == 1


async def test_a_family_is_one_result(store):
    rows = await search(store, "ridgeline", None, limit=8)
    assert [r.product.name for r in rows].count("Ridgeline 2-Person Tent") == 1


async def test_filters_narrow_by_category_and_price(store):
    rows = await search(store, "", SearchFilters(category="camping", max_price=100.0), limit=8)
    assert all(r.merch.category == "camping" for r in rows)
    assert all(r.variant.price <= 100.0 for r in rows)


async def test_sort_by_price_ascending(store):
    rows = await search(store, "", SearchFilters(sort="price_asc"), limit=8)
    prices = [r.variant.price for r in rows]
    assert prices == sorted(prices)


async def test_no_match_returns_empty(store):
    assert await search(store, "submarine", None, limit=8) == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_search.py -v`
Expected: FAIL — no module `engine_backend.search`.

- [ ] **Step 3: Implement it**

```python
# engine_backend/search.py
"""Deterministic keyword and facet search.

The engine's semantic search needs an OpenAI key (Commerce.vector(openai_api_key)), which
a Claude reference will not require, so matching here is explicit and inspectable. A
deployment with its own search service replaces this module and nothing else.
"""

from __future__ import annotations

from shopping_agent.types import SearchFilters

from engine_backend.catalog import CatalogRow, catalog_rows
from engine_backend.store import EngineStore


def _haystack(row: CatalogRow) -> str:
    parts = [
        row.product.name,
        row.product.description or "",
        row.variant.name or "",
        row.merch.brand or "",
        row.merch.category or "",
        " ".join(row.merch.labels),
        " ".join(f"{k} {v}" for k, v in row.merch.attributes.items()),
    ]
    return " ".join(parts).lower()


def score(row: CatalogRow, terms: list[str]) -> float:
    """2 points for a term in the product name, 1 for anywhere else."""
    if not terms:
        return 1.0
    name = row.product.name.lower()
    hay = _haystack(row)
    total = 0.0
    for term in terms:
        if term in name:
            total += 2.0
        elif term in hay:
            total += 1.0
    return total


def _passes(row: CatalogRow, filters: SearchFilters) -> bool:
    if filters.category and row.merch.category != filters.category:
        return False
    if filters.min_price is not None and row.variant.price < filters.min_price:
        return False
    if filters.max_price is not None and row.variant.price > filters.max_price:
        return False
    if filters.min_rating is not None and (row.merch.rating or 0.0) < filters.min_rating:
        return False
    return all(row.merch.attributes.get(k) == v for k, v in filters.attributes.items())


async def search(
    store: EngineStore, query: str, filters: SearchFilters | None, limit: int
) -> list[CatalogRow]:
    filters = filters or SearchFilters()
    terms = [t for t in query.lower().split() if t]
    rows = [r for r in await catalog_rows(store) if _passes(r, filters)]
    if terms:
        rows = [r for r in rows if score(r, terms) > 0]

    # One row per family: the cheapest in-stock variant represents it.
    by_family: dict[str, CatalogRow] = {}
    for row in sorted(rows, key=lambda r: (r.stock <= 0, r.variant.price)):
        by_family.setdefault(row.product.id, row)
    chosen = list(by_family.values())

    if filters.sort == "price_asc":
        chosen.sort(key=lambda r: r.variant.price)
    elif filters.sort == "price_desc":
        chosen.sort(key=lambda r: -r.variant.price)
    elif filters.sort == "rating":
        chosen.sort(key=lambda r: -(r.merch.rating or 0.0))
    else:
        chosen.sort(key=lambda r: (-score(r, terms), r.variant.price))
    return chosen[:limit]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_search.py -v`
Expected: PASS, five tests.

- [ ] **Step 5: Commit**

```bash
git add engine_backend/search.py tests/test_search.py
git commit -m "Add deterministic catalog search over the engine"
```

---

### Task 5: StorefrontBackend — catalog and cart

**Files:**
- Create: `engine_backend/storefront.py`
- Test: `tests/test_storefront_catalog.py`, `tests/test_storefront_cart.py`

**Interfaces:**
- Consumes: `EngineStore`, `search`, `catalog_rows`, `list_variants`, `read_merchandising`.
- Produces: `class EngineStorefront(StorefrontBackend)` with `__init__(self, store: EngineStore, checkout_base_url: str | None = None)`, plus module functions `to_product(row: CatalogRow) -> shopping_agent.types.Product` and `to_family(product, variants, merch, rows) -> shopping_agent.types.Product`.

- [ ] **Step 1: Write the failing catalog test**

```python
# tests/test_storefront_catalog.py
from shopping_agent.types import ShoppingSessionContext

from engine_backend.storefront import EngineStorefront


def session(store):
    store.bind("sess-1", _customer_id(store), "customer")
    return ShoppingSessionContext(session_id="sess-1", user_id="rowan")


def _customer_id(store):
    return store.commerce.customers.list()[0].id


async def test_search_returns_upstream_products(store):
    backend = EngineStorefront(store)
    results = await backend.search_products(session(store), "tent")
    assert results
    first = results[0]
    assert first.title == "Ridgeline 2-Person Tent"
    assert first.price > 0
    assert first.currency == "USD"
    assert first.options == {"colour": ["green", "tan"]}
    assert first.product_id == store.commerce.products.list()[0].id or first.product_id


async def test_details_of_a_family_carry_its_variants(store):
    backend = EngineStorefront(store)
    ctx = session(store)
    family_id = (await backend.search_products(ctx, "tent"))[0].product_id
    details = await backend.get_product_details(ctx, family_id)
    assert details is not None
    assert {v.product_id for v in details.variants} == {"TENT-RIDGE-GRN", "TENT-RIDGE-TAN"}
    assert all(v.variant_of == family_id for v in details.variants)
    assert details.specs["packed_weight"] == "2.4 kg"


async def test_details_of_a_variant_sku_returns_that_variant(store):
    backend = EngineStorefront(store)
    details = await backend.get_product_details(session(store), "TENT-RIDGE-TAN")
    assert details is not None
    assert details.option_values == {"colour": "tan"}
    assert details.variants == []


async def test_details_of_an_unknown_id_is_none(store):
    backend = EngineStorefront(store)
    assert await backend.get_product_details(session(store), "NOPE-1") is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_storefront_catalog.py -v`
Expected: FAIL — no module `engine_backend.storefront`.

- [ ] **Step 3: Implement the catalog half**

Write `to_product` mapping a `CatalogRow` to `shopping_agent.types.Product` with `product_id=row.variant.sku`, `title=f"{product.name}"` (append the variant name only when the family has more than one variant), `price=row.variant.price`, `in_stock=row.stock > 0`, and every merchandising field copied across. Write `to_family` producing the family record: `product_id=product.id`, `price` the lowest variant price, `options` built from `merch.option_names` and `merch.variant_options`, `in_stock` true when any variant has stock.

`search_products` maps `search(...)` results through `to_family` when the product has more than one variant and `to_product` otherwise. `get_product_details` accepts either a product id or a SKU: for a product id return the family with `variants=[to_product(...)]` per variant; for a SKU return that variant with `variant_of` set and `variants=[]`; unknown ids return `None`.

- [ ] **Step 4: Run the catalog tests**

Run: `.venv/bin/python -m pytest tests/test_storefront_catalog.py -v`
Expected: PASS, four tests.

- [ ] **Step 5: Write the failing cart test**

```python
# tests/test_storefront_cart.py
from shopping_agent.types import ShoppingSessionContext

from engine_backend.storefront import EngineStorefront


def session(store):
    store.bind("sess-1", store.commerce.customers.list()[0].id, "customer")
    return ShoppingSessionContext(session_id="sess-1", user_id="rowan")


async def test_cart_starts_empty(store):
    cart = await EngineStorefront(store).get_cart(session(store))
    assert cart.items == []


async def test_add_update_and_remove_by_sku(store):
    backend = EngineStorefront(store)
    ctx = session(store)

    cart = await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 2)
    assert [(i.product_id, i.quantity) for i in cart.items] == [("TENT-RIDGE-GRN", 2)]
    assert cart.items[0].option_values == {"colour": "green"}

    cart = await backend.update_cart_item(ctx, "TENT-RIDGE-GRN", 1)
    assert cart.items[0].quantity == 1

    cart = await backend.remove_from_cart(ctx, "TENT-RIDGE-GRN")
    assert cart.items == []


async def test_updating_a_line_the_cart_does_not_hold_is_a_no_op(store):
    backend = EngineStorefront(store)
    ctx = session(store)
    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1)
    cart = await backend.update_cart_item(ctx, "TENT-RIDGE-TAN", 3)
    assert [(i.product_id, i.quantity) for i in cart.items] == [("TENT-RIDGE-GRN", 1)]


async def test_the_cart_persists_in_the_engine(store):
    backend = EngineStorefront(store)
    ctx = session(store)
    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1)
    engine_carts = store.commerce.carts.list()
    assert len(engine_carts) == 1
    assert store.commerce.carts.get_items(engine_carts[0].id)[0].sku == "TENT-RIDGE-GRN"
```

- [ ] **Step 6: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_storefront_cart.py -v`
Expected: FAIL — `NotImplementedError` or `AttributeError` on the cart methods.

- [ ] **Step 7: Implement the cart half**

One open engine cart per session, created lazily by `_cart_id(session)` and remembered on the backend keyed by `session.session_id`. `add_to_cart` resolves the SKU with `products.get_variant_by_sku`, adds via `AddCartItemInput(sku=..., name=..., quantity=..., unit_price=variant.price, product_id=..., variant_id=...)`, and when the SKU is already a line calls `update_item` with the summed quantity instead. `update_cart_item` and `remove_from_cart` find the engine `CartItem` by `sku` and no-op when absent. Every cart method returns `_to_cart(...)`, built from `carts.get_items` with `option_values` read from the product's merchandising object. All writes go through `store.write(session.session_id, ...)`.

- [ ] **Step 8: Run the cart tests**

Run: `.venv/bin/python -m pytest tests/test_storefront_cart.py -v`
Expected: PASS, four tests.

- [ ] **Step 9: Commit**

```bash
git add engine_backend/storefront.py tests/test_storefront_catalog.py tests/test_storefront_cart.py
git commit -m "Implement StorefrontBackend catalog and cart over the engine"
```

---

### Task 6: StorefrontBackend — orders, profile, policies, fulfillment, handoff

**Files:**
- Modify: `engine_backend/storefront.py`
- Create: `engine_backend/content.py`
- Test: `tests/test_storefront_orders.py`, `tests/test_content.py`

**Interfaces:**
- Consumes: `EngineStore`, `EngineStorefront`.
- Produces: in `content.py`, `POLICY_TYPE = "policy_document"`, `DISCLOSURE_TYPE = "disclosure"`, `seed_content(commerce) -> None`, `async find_policies(store, query) -> list[shopping_agent.types.Policy]`, `async find_disclosure(store, product_id) -> shopping_agent.types.Disclosure | None`. `EngineStorefront` gains the remaining nine methods.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storefront_orders.py
from shopping_agent.types import ShoppingSessionContext

from engine_backend.storefront import EngineStorefront


def session(store):
    store.bind("sess-1", store.commerce.customers.list()[0].id, "customer")
    return ShoppingSessionContext(session_id="sess-1", user_id="rowan")


async def test_orders_are_the_sessions_own(store):
    orders = await EngineStorefront(store).get_orders(session(store))
    assert len(orders) == 1
    assert orders[0].items[0].product_id == "TENT-RIDGE-GRN"
    assert orders[0].total == 219.00


async def test_another_customers_order_is_not_returned(store):
    other = store.commerce.customers.create(
        email="stranger@example.invalid", first_name="Sam", last_name="Vale"
    )
    store.bind("sess-2", other.id, "customer")
    ctx = ShoppingSessionContext(session_id="sess-2", user_id="sam")
    backend = EngineStorefront(store)
    mine = store.commerce.orders.list()[0]
    assert await backend.get_orders(ctx) == []
    assert await backend.get_order(ctx, mine.id) is None


async def test_preferences_come_from_the_bound_customer(store):
    prefs = await EngineStorefront(store).get_preferences(session(store))
    assert prefs.display_name == "Rowan Ellis"


async def test_fulfillment_options_come_from_the_engine(store):
    options = await EngineStorefront(store).get_fulfillment_options(
        session(store), ["TENT-RIDGE-GRN"]
    )
    assert isinstance(options, list)


async def test_checkout_handoff_returns_a_host_url(store):
    backend = EngineStorefront(store, checkout_base_url="http://localhost:8000/checkout")
    ctx = session(store)
    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1)
    cart = await backend.get_cart(ctx)
    handoffs = await backend.checkout_handoff(ctx, cart)
    assert handoffs and handoffs[0].url.startswith("http://localhost:8000/checkout")
```

```python
# tests/test_content.py
from engine_backend.content import find_disclosure, find_policies, seed_content


async def test_policies_are_searchable(store):
    seed_content(store.commerce)
    hits = await find_policies(store, "return")
    assert hits and "return" in hits[0].title.lower()
    assert await find_policies(store, "cryptocurrency") == []


async def test_a_disclosure_is_server_authored(store):
    seed_content(store.commerce)
    disclosure = await find_disclosure(store, "TENT-RIDGE-GRN")
    assert disclosure is not None
    assert disclosure.rows
```

- [ ] **Step 2: Run both and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_storefront_orders.py tests/test_content.py -v`
Expected: FAIL — no module `engine_backend.content`, and the order methods still abstract.

- [ ] **Step 3: Implement `content.py`**

Two custom object types, seeded with at least four fictional policy documents (returns, shipping, warranty, privacy) each with `policy_id`, `title`, `body`, and one disclosure per product. `find_policies` lower-cases the query, splits it into terms, and returns documents whose title or body contains a term, best first by hit count. `find_disclosure` looks the object up by `owner_id` and returns `None` when absent — the model never authors a row.

- [ ] **Step 4: Implement the remaining backend methods**

`get_orders` and `get_order` filter `orders.list()` by the bound customer id and map to `shopping_agent.types.Order`, with `product_id` taken from the order item's SKU, `status` mapped from the engine's status string to `OrderStatus`, and `tracking_url` built from `order.tracking_number` when set. `get_preferences` reads the bound customer. `get_account_context` returns `None`. `search_policies` and `get_disclosure` delegate to `content.py`. `get_fulfillment_options` calls `carts.get_shipping_rates` on the session cart when one exists and returns `[]` otherwise, capped at the first twenty ids. `checkout_handoff` returns one `CheckoutHandoff` with `url=f"{checkout_base_url}?cart={cart_id}"` when `checkout_base_url` is set, and `[]` otherwise.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_storefront_orders.py tests/test_content.py -v`
Expected: PASS, seven tests.

- [ ] **Step 6: Assert the interface is fully implemented**

```python
# append to tests/test_storefront_orders.py
def test_no_abstract_methods_remain():
    from engine_backend.storefront import EngineStorefront

    assert EngineStorefront.__abstractmethods__ == frozenset()
```

Run: `.venv/bin/python -m pytest tests/test_storefront_orders.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add engine_backend/content.py engine_backend/storefront.py tests/
git commit -m "Complete StorefrontBackend: orders, profile, policies, fulfillment, handoff"
```

---

### Task 7: The governed kernel seam

**Files:**
- Create: `engine_backend/kernel.py`, `config/kernel-policy.json`, `config/kernel-principal.json`
- Test: `tests/test_kernel.py`

**Interfaces:**
- Consumes: `EngineStore`.
- Produces:
  - `class Receipt(BaseModel)`: `receipt_id: str`, `command_id: str`, `command_type: str`, `status: str`, `idempotency_key: str`, `result: dict | None`, `error_code: str | None`, `error_message: str | None`; `Receipt.ok -> bool` (`status == "succeeded"`).
  - `class KernelClient` with `__init__(self, store: EngineStore, policy_path: Path, principal_path: Path)`,
  - `async KernelClient.execute(command_type: str, payload: dict, idempotency_key: str, approval: dict | None = None) -> Receipt`,
  - `def approval_evidence(approval_id: str, approved_by: str, scope: str, store_id: str) -> dict`.

- [ ] **Step 1: Write the config files**

Copy `vendor/commerce-agents`-independent config from the engine's `kernel/examples/strict-policy.json` and `strict-principal.json`, replacing every `replace-me` with this repo's values: tenant `tenant:acme`, store `store:acme`, principal `agent:acme-merchant` delegated by `user:acme-operator`. Keep `requires_approval: true` on `payments.create_refund` and set it `false` on the others.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_kernel.py
from pathlib import Path

import pytest

from engine_backend.kernel import KernelClient, approval_evidence

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")


async def test_a_governed_payment_returns_a_sealed_receipt(kernel, store):
    receipt = await kernel.execute(
        "payments.create",
        {"amount": "12.34", "currency": "USD", "payment_method": "credit_card"},
        idempotency_key="test-payment-1",
    )
    assert receipt.ok
    assert receipt.command_type == "payments.create"
    assert receipt.idempotency_key == "test-payment-1"
    assert receipt.receipt_id


async def test_a_refund_without_approval_evidence_is_refused(kernel, store):
    payment = store.commerce.payments.list()[0]
    receipt = await kernel.execute(
        "payments.create_refund",
        {"payment_id": payment.id, "amount": "10.00"},
        idempotency_key="test-refund-noapproval",
    )
    assert not receipt.ok
    assert receipt.error_code


async def test_an_over_refund_is_refused_even_with_approval(kernel, store):
    payment = store.commerce.payments.list()[0]
    receipt = await kernel.execute(
        "payments.create_refund",
        {"payment_id": payment.id, "amount": "10000.00"},
        idempotency_key="test-refund-toolarge",
        approval=approval_evidence(
            "appr-1", "user:acme-operator", "payments.create_refund", "store:acme"
        ),
    )
    assert not receipt.ok
    assert receipt.error_code
    assert receipt.error_message
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_kernel.py -v`
Expected: FAIL — no module `engine_backend.kernel`.

- [ ] **Step 4: Implement it**

```python
# engine_backend/kernel.py
"""The governed write seam.

A governed command is an envelope (contract_version, command_id, idempotency_key,
command_type, principal, store_id, optional approval) executed by
Commerce.execute_kernel_command against host-owned policy. The policy and the principal
are files on disk, never model input; the return is a sealed receipt whose error_code is a
stable code, never prose to parse.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from stateset_embedded import Commerce

from engine_backend.store import EngineStore

CONTRACT_VERSION = "1.0"


class Receipt(BaseModel):
    receipt_id: str = ""
    command_id: str = ""
    command_type: str = ""
    status: str = ""
    idempotency_key: str = ""
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def approval_evidence(approval_id: str, approved_by: str, scope: str, store_id: str) -> dict:
    return {
        "approval_id": approval_id,
        "approved_by": approved_by,
        "scope": scope,
        "store_id": store_id,
        "approved_at": datetime.now(UTC).isoformat(),
    }


class KernelClient:
    def __init__(self, store: EngineStore, policy_path: Path, principal_path: Path) -> None:
        self.store = store
        self.policy = json.loads(Path(policy_path).read_text())
        self.principal = json.loads(Path(principal_path).read_text())

    async def execute(
        self,
        command_type: str,
        payload: dict,
        idempotency_key: str,
        approval: dict | None = None,
    ) -> Receipt:
        envelope = {
            "contract_version": CONTRACT_VERSION,
            "command_id": str(uuid.uuid4()),
            "idempotency_key": idempotency_key,
            "command_type": command_type,
            "principal": self.principal,
            "store_id": self.store.store_id,
            "policy_version": self.policy.get("version"),
            "payload": payload,
        }
        if approval is not None:
            envelope["approval"] = approval
        command_json = json.dumps(envelope)
        policy_json = json.dumps(self.policy)

        def body(c: Commerce) -> str:
            return c.execute_kernel_command(command_json, policy_json)

        try:
            raw = await self.store.write("kernel", body)
        except Exception as error:  # a refusal the binding raises rather than seals
            return Receipt(
                command_type=command_type,
                idempotency_key=idempotency_key,
                status="failed",
                error_code="kernel.rejected",
                error_message=str(error),
            )
        return Receipt.model_validate(json.loads(raw))
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_kernel.py -v`
Expected: PASS, three tests. If the envelope is rejected as malformed, print the raised message — it names the field — and correct the envelope. Do not relax the policy to make a test pass; a refusal is the behavior under test.

- [ ] **Step 6: Commit**

```bash
git add engine_backend/kernel.py config/ tests/test_kernel.py
git commit -m "Add the governed kernel seam and its receipts"
```

---

### Task 8: Staged changes that persist in the engine

**Files:**
- Create: `engine_backend/staging.py`
- Test: `tests/test_staging.py`

**Interfaces:**
- Consumes: `EngineStore`.
- Produces: `STAGED_TYPE = "staged_change"`, `async save(store, change: StagedChange) -> None`, `async load(store, change_id: str) -> StagedChange | None`, `async pending(store) -> list[StagedChange]`, `def new_change(kind, summary, items, operator, currency=None, guardrail_notes=None) -> StagedChange`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_staging.py
from merchant_agent.types import ChangeKind, ChangeStatus

from engine_backend.staging import load, new_change, pending, save


async def test_a_staged_change_round_trips(store):
    change = new_change(
        kind=ChangeKind.PRICE_UPDATE,
        summary="Drop the tan tent to 199.00",
        items=[],
        operator="user:acme-operator",
    )
    await save(store, change)
    loaded = await load(store, change.change_id)
    assert loaded is not None
    assert loaded.summary == change.summary
    assert loaded.status is ChangeStatus.STAGED
    assert loaded.created_by == "user:acme-operator"


async def test_pending_excludes_applied_and_discarded(store):
    staged = new_change(ChangeKind.PRICE_UPDATE, "staged", [], "user:acme-operator")
    applied = new_change(ChangeKind.PRICE_UPDATE, "applied", [], "user:acme-operator")
    applied.status = ChangeStatus.APPLIED
    await save(store, staged)
    await save(store, applied)
    assert [c.summary for c in await pending(store)] == ["staged"]


async def test_load_of_an_unknown_id_is_none(store):
    assert await load(store, "chg-nope") is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_staging.py -v`
Expected: FAIL — no module `engine_backend.staging`.

- [ ] **Step 3: Implement it**

Persist each change as a custom object of type `staged_change` with `handle=change.change_id` and `values_json={"payload": change.model_dump(mode="json")}`. `save` creates or updates by handle; `load` uses `get_object_by_handle`; `pending` lists the type and filters `status == "staged"`. `new_change` mints `change_id=f"chg-{uuid4().hex[:12]}"`, sets `created_at=datetime.now(UTC)`, and stamps `created_by`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_staging.py -v`
Expected: PASS, three tests.

- [ ] **Step 5: Commit**

```bash
git add engine_backend/staging.py tests/test_staging.py
git commit -m "Persist staged changes as engine custom objects"
```

---

### Task 9: MerchantBackend — the reads

**Files:**
- Create: `engine_backend/merchant.py`
- Test: `tests/test_merchant_reads.py`

**Interfaces:**
- Consumes: `EngineStore`, `catalog_rows`, `list_variants`, `read_merchandising`, `KernelClient`.
- Produces: `class EngineMerchant(MerchantBackend)` with `__init__(self, store: EngineStore, kernel: KernelClient)`; module function `to_listing(row: CatalogRow) -> merchant_agent.types.Listing`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merchant_reads.py
from merchant_agent.types import MerchantSessionContext

from engine_backend.merchant import EngineMerchant


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def test_snapshot_figures_come_from_the_engine(store, kernel):
    snapshot = await EngineMerchant(store, kernel).get_business_snapshot(session())
    assert snapshot.revenue is not None


async def test_listings_carry_stock_and_price(store, kernel):
    listings = await EngineMerchant(store, kernel).search_listings(session(), "tent")
    assert listings
    assert listings[0].price > 0
    assert listings[0].stock >= 0


async def test_listing_details_of_a_family_carry_variants(store, kernel):
    backend = EngineMerchant(store, kernel)
    family_id = (await backend.search_listings(session(), "tent"))[0].listing_id
    details = await backend.get_listing(session(), family_id)
    assert details is not None
    assert {v.listing_id for v in details.variants} == {"TENT-RIDGE-GRN", "TENT-RIDGE-TAN"}


async def test_inventory_alerts_flag_the_low_sku(store, kernel):
    alerts = await EngineMerchant(store, kernel).get_inventory_alerts(session())
    assert any(a.listing_id == "TENT-RIDGE-TAN" for a in alerts)


async def test_pricing_context_reports_unit_cost(store, kernel):
    context = await EngineMerchant(store, kernel).get_pricing_context(
        session(), "TENT-RIDGE-GRN"
    )
    assert context is not None
    assert context.items[0].unit_cost == 128.00


async def test_analysis_query_is_select_only_and_capped(store, kernel):
    backend = EngineMerchant(store, kernel)
    table = await backend.execute_analysis_query(session(), "SELECT COUNT(*) AS n FROM orders")
    assert table is not None and table.rows
    assert await backend.get_analysis_schema(session())
    import pytest

    with pytest.raises(Exception):
        await backend.execute_analysis_query(session(), "DELETE FROM orders")
```

Add to `tests/conftest.py`:

```python
from pathlib import Path

from engine_backend.kernel import KernelClient

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_merchant_reads.py -v`
Expected: FAIL — no module `engine_backend.merchant`.

- [ ] **Step 3: Implement the reads**

`get_business_snapshot` maps `analytics.sales_summary` and `analytics.customer_metrics`; a figure the engine cannot supply is `None` with a `note`, never zero. `query_metrics` maps `analytics.revenue_by_period` for revenue and `analytics.top_products` for units; an unsupported metric returns an empty series with a `note`. `get_campaign_performance` reads the `campaign` custom objects and returns `[]` when none exist. `search_listings` and `get_listing` reuse `search` and `catalog_rows` through `to_listing`, with `content_quality` derived from whether the merchandising object has a `long_description` and at least one spec. `get_inventory_alerts` maps `analytics.low_stock_items`. `get_order_issues` maps `analytics.order_status_breakdown` plus orders stuck unfulfilled. `get_pricing_context` reads price and `unit_cost` from the variant and its merchandising object. `execute_analysis_query` opens `store.readonly_sql()`, runs the single statement, caps at 100 rows and 8000 characters, and lets the read-only connection reject writes. `get_analysis_schema` returns a short fenced description of the tables the demo query surface uses. `get_merchant_context` returns the store name, the reporting period, and a `limitations` entry stating that campaigns are not managed by the engine.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_merchant_reads.py -v`
Expected: PASS, six tests.

- [ ] **Step 5: Commit**

```bash
git add engine_backend/merchant.py tests/test_merchant_reads.py tests/conftest.py
git commit -m "Implement the MerchantBackend reads over engine analytics"
```

---

### Task 10: MerchantBackend — staged writes and apply

**Files:**
- Modify: `engine_backend/merchant.py`
- Test: `tests/test_merchant_writes.py`

**Interfaces:**
- Consumes: `staging`, `KernelClient`, `EngineStore`.
- Produces: the remaining seven `MerchantBackend` methods, plus `EngineMerchant.approve(change_id: str, approved_by: str) -> None` used by the host's approval route, and `EngineMerchant.approved_ids -> set[str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_merchant_writes.py
import pytest
from merchant_agent.types import ChangeStatus, MerchantSessionContext, PriceUpdateItem

from engine_backend.merchant import EngineMerchant


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def test_staging_a_price_update_changes_nothing_live(store, kernel):
    backend = EngineMerchant(store, kernel)
    before = store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    assert change.status is ChangeStatus.STAGED
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price == before
    assert [c.change_id for c in await backend.get_pending_changes(session())] == [
        change.change_id
    ]


async def test_apply_without_approval_is_refused(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    with pytest.raises(Exception):
        await backend.apply_change(session(), change.change_id)
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price != 199.00


async def test_an_approved_price_update_writes_and_logs(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    assert applied.applied_by == "user:acme-operator"
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price == 199.00
    logs = store.commerce.activity_logs.list(subject_type="staged_change", limit=10)
    assert any(entry.subject_id == change.change_id for entry in logs)


async def test_a_restock_of_a_new_sku_is_governed(store, kernel):
    from merchant_agent.types import InventoryActionItem

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_inventory_action(
        session(), [InventoryActionItem(listing_id="TENT-RIDGE-TAN", action="restock", quantity=20)]
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)
    assert applied.status is ChangeStatus.APPLIED
    assert store.commerce.inventory.get_stock("TENT-RIDGE-TAN").total_available >= 24


async def test_discard_leaves_live_state_alone(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    discarded = await backend.discard_change(session(), change.change_id)
    assert discarded.status is ChangeStatus.DISCARDED
    assert await backend.get_pending_changes(session()) == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_merchant_writes.py -v`
Expected: FAIL — the `stage_*` methods are still abstract.

- [ ] **Step 3: Implement the writes**

Each `stage_*` builds `ChangeItem` rows from live reads (never from the tool argument alone), runs upstream's `merchant_agent.changes.check_guardrails`, and persists through `staging.save`. `apply_change` refuses a change whose id is not in `approved_ids` and refuses one that is not `STAGED`; it then dispatches on `ChangeKind`:

- `PRICE_UPDATE`: update the variant price through the engine, then `activity_logs.record(subject_type="staged_change", subject_id=change_id, action="apply", actor_kind="user", actor=operator)`.
- `INVENTORY_ACTION`: a `restock` of a SKU with no inventory item goes through `kernel.execute("inventory.item.create", ...)` and records the receipt id in `guardrail_notes`; a restock of an existing SKU is `inventory.adjust`; `pause` and `activate` set the product status. Only the first is governed — the docstring says so.
- `LISTING_UPDATE`: write the merchandising object and the product description.
- `PROMOTION` and `CAMPAIGN`: write the corresponding custom object.

A failed write leaves the change `STAGED` and raises. `discard_change` sets `DISCARDED`, stamps `discarded_by` and `discarded_by_kind`, and saves.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_merchant_writes.py -v`
Expected: PASS, five tests.

- [ ] **Step 5: Assert the interface is complete**

```python
# append to tests/test_merchant_writes.py
def test_no_abstract_methods_remain():
    from engine_backend.merchant import EngineMerchant

    assert EngineMerchant.__abstractmethods__ == frozenset()
```

Run: `.venv/bin/python -m pytest tests/test_merchant_writes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add engine_backend/merchant.py tests/test_merchant_writes.py
git commit -m "Implement staged merchant writes, approval, and apply dispatch"
```

---

### Task 11: The FastAPI host

**Files:**
- Create: `host/__init__.py`, `host/sessions.py`, `host/app.py`, `scripts/run_demo.py`
- Test: `tests/test_host.py`

**Interfaces:**
- Consumes: `EngineStorefront`, `EngineMerchant`, `KernelClient`, `SKILLS_DIR`.
- Produces: `create_app(db_path: str) -> fastapi.FastAPI` exposing `POST /shopping/session`, `POST /shopping/chat`, `POST /shopping/checkout`, `POST /merchant/session`, `POST /merchant/chat`, `POST /merchant/changes/{change_id}/approve`, `GET /healthz`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_host.py
from fastapi.testclient import TestClient

from host.app import create_app


def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "store.db")))


def test_health(tmp_path):
    assert client(tmp_path).get("/healthz").json()["status"] == "ok"


def test_a_session_binds_identity_server_side(tmp_path):
    c = client(tmp_path)
    body = c.post("/shopping/session").json()
    assert body["session_id"]
    assert "customer_id" not in body


def test_checkout_completes_the_cart_through_the_engine(tmp_path):
    c = client(tmp_path)
    session_id = c.post("/shopping/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}
    c.post("/shopping/cart/add", json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
           headers=headers)
    result = c.post("/shopping/checkout", headers=headers).json()
    assert result["order_number"]
    assert result["receipt"]["status"] == "succeeded"


def test_approving_a_change_requires_a_known_id(tmp_path):
    c = client(tmp_path)
    session_id = c.post("/merchant/session").json()["session_id"]
    response = c.post(
        "/merchant/changes/chg-nope/approve", headers={"X-Session-Id": session_id}
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_host.py -v`
Expected: FAIL — no module `host.app`.

- [ ] **Step 3: Implement the host**

`create_app` builds one `EngineStore`, seeds it, and constructs both backends and a `KernelClient`. A session route mints an unguessable id (`secrets.token_urlsafe(24)`), binds it to a customer or the operator principal, and returns only the id. Every other route reads `X-Session-Id` and 401s when unbound. The chat routes construct `ShoppingAgent` / `MerchantAgent` with `skills_dir=SKILLS_DIR(role)` and stream the upstream event types as server-sent events. `POST /shopping/cart/add` exists for tests and the web app and calls the backend directly. `POST /shopping/checkout` calls `kernel.execute("checkout.commit", {"cart_id": ...}, idempotency_key=f"checkout-{cart_id}")` and returns the order number with the receipt — this is the only place a cart is completed, and a human click is what reaches it. The approve route calls `merchant.approve(change_id, operator)` after confirming the change exists, and 404s otherwise.

- [ ] **Step 4: Write `scripts/run_demo.py`**

Starts uvicorn on :8000 and, with `--web`, `npm run dev` for both web apps.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_host.py -v`
Expected: PASS, four tests.

- [ ] **Step 6: Commit**

```bash
git add host/ scripts/run_demo.py tests/test_host.py
git commit -m "Add the FastAPI host for both roles, with governed checkout"
```

---

### Task 12: The two MCP servers

**Files:**
- Create: `mcp_servers/__init__.py`, `mcp_servers/shopping.py`, `mcp_servers/merchant.py`
- Test: `tests/test_mcp_servers.py`

**Interfaces:**
- Consumes: `commerce_common.mcp_server`, both backends.
- Produces: `build_shopping_server(db_path: str)` and `build_merchant_server(db_path: str)`, each returning the upstream server object; both bind to loopback unless `COMMERCE_MCP_BEHIND_GATEWAY=1`.

- [ ] **Step 1: Read the upstream module first**

Run: `sed -n '1,120p' vendor/commerce-agents/commerce-common/commerce_common/mcp_server.py`
Use whatever constructor and bind-guard function it defines; do not invent one.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_mcp_servers.py
def test_the_tool_surface_is_the_role_surface_not_the_engines(tmp_path):
    from mcp_servers.shopping import build_shopping_server

    server = build_shopping_server(str(tmp_path / "store.db"))
    names = {tool.name for tool in server.list_tools()}
    assert "search_products" in names
    assert len(names) < 40, "the role surface is ~20 tools, not the engine's 900"


def test_the_merchant_server_exposes_apply_change(tmp_path):
    from mcp_servers.merchant import build_merchant_server

    server = build_merchant_server(str(tmp_path / "store.db"))
    assert "apply_change" in {tool.name for tool in server.list_tools()}
```

Adjust `server.list_tools()` to the accessor the upstream module actually provides, discovered in Step 1.

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_servers.py -v`
Expected: FAIL — no module `mcp_servers.shopping`.

- [ ] **Step 4: Implement both servers**

Each builds the store, seeds it, constructs the backend, and hands it to the upstream server builder with the role's config and `skills_dir`. The principal comes from the environment (`ACME_OPERATOR`, `ACME_CUSTOMER`), never from a tool argument.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_servers.py -v`
Expected: PASS, two tests.

- [ ] **Step 6: Commit**

```bash
git add mcp_servers/ tests/test_mcp_servers.py
git commit -m "Add role MCP servers over the same backends and gates"
```

---

### Task 13: The two web apps

**Files:**
- Create: `package.json` (workspace root), `web/storefront/`, `web/portal/`
- Test: `tests/test_web_build.py`

**Interfaces:**
- Consumes: the host's routes; the submodule's `examples/web-shared`.
- Produces: a storefront on :3000 and a portal on :3100.

- [ ] **Step 1: Settle the shared-component question first**

Run:
```bash
cd /home/dom/stateset-icommerce-agents
cat > package.json <<'JSON'
{
  "name": "stateset-icommerce-agents-web",
  "private": true,
  "workspaces": ["web/*", "vendor/commerce-agents/examples/web-shared"]
}
JSON
npm install
npm ls web-shared || true
```
If `web-shared` resolves and builds, use it. If it does not, take the fallback from the spec: two minimal apps with no shared component dependency. Record which path was taken in `docs/mapping.md` and move on — do not spend more than one attempt on this.

- [ ] **Step 2: Write the failing build test**

```python
# tests/test_web_build.py
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("app", ["storefront", "portal"])
def test_the_web_app_builds(app):
    if not (ROOT / "node_modules").is_dir():
        pytest.skip("npm install has not run")
    result = subprocess.run(
        ["npm", "run", "build", "--workspace", f"web/{app}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_web_build.py -v`
Expected: FAIL — the workspaces do not exist yet.

- [ ] **Step 4: Build the storefront**

A Next.js app with one chat page: a message list, an input, an SSE reader for the host's `/shopping/chat` stream, product cards rendered from `ui` events, a cart panel fed by `cart_update`, and a Place order button that POSTs `/shopping/checkout` and shows the returned order number and receipt status. The session id is fetched once from `/shopping/session` and sent as `X-Session-Id`.

- [ ] **Step 5: Build the portal**

The same shape against `/merchant/*`, with a staged-changes panel fed by `change_update` and an Approve button per change that POSTs the approve route, then an Apply that runs through chat. Show each applied change's evidence — a kernel receipt id, or the activity-log id — so the two enforcement layers are visible on screen.

- [ ] **Step 6: Run the build test**

Run: `.venv/bin/python -m pytest tests/test_web_build.py -v`
Expected: PASS, two tests.

- [ ] **Step 7: Commit**

```bash
git add package.json package-lock.json web/ tests/test_web_build.py
git commit -m "Add the storefront and portal web apps"
```

---

### Task 14: Documentation, the denial demo, and the drift check

**Files:**
- Create: `README.md`, `docs/enforcement.md`, `docs/mapping.md`, `docs/install.md`, `scripts/denials.py`, `scripts/check.py`, `LICENSE`
- Test: `tests/test_denials.py`, `tests/test_check.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `scripts/denials.py` printing three denials; `scripts/check.py` exiting non-zero on drift.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_denials.py
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_all_three_denials_are_demonstrated(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/denials.py", "--db", str(tmp_path / "store.db")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    out = result.stdout
    assert "DENIED (agent layer)" in out
    assert out.count("DENIED") == 3
    assert "receipt" in out.lower()
```

```python
# tests/test_check.py
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_check_passes_on_a_clean_tree():
    result = subprocess.run(
        [sys.executable, "scripts/check.py"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest tests/test_denials.py tests/test_check.py -v`
Expected: FAIL — neither script exists.

- [ ] **Step 3: Write `scripts/denials.py`**

Three sections, each printing what was attempted, the refusal, and the evidence:

1. Build a `ShoppingSessionState` with no `seen_products`, call the shopping executor's `add_to_cart` for `TENT-RIDGE-GRN`, and print the `blocked` outcome and the gate name. Prefix `DENIED (agent layer)`.
2. Stage a price update, skip `approve`, call `apply_change`, print the refusal. Prefix `DENIED (agent layer)`.
3. Issue `payments.create_refund` for more than the captured amount through `KernelClient` with valid approval evidence, and print the receipt's `status`, `error_code`, and `receipt_id`. Prefix `DENIED (engine, in transaction)`.

- [ ] **Step 4: Write `scripts/check.py`**

Exits non-zero when: either backend has a non-empty `__abstractmethods__`; the submodule commit differs from the one recorded in `docs/mapping.md`; or any read-only SQL fallback exists in `engine_backend/` that `docs/mapping.md` does not list. Record the submodule commit in `docs/mapping.md` as a fenced line the script parses.

- [ ] **Step 5: Write the documentation**

`docs/enforcement.md` opens with the doubled approval rule, then one table with a row per write this repo can perform: the write, the agent-layer gate, the engine-layer command or `none`, and the evidence returned. It states plainly that the engine governs the money and the stock ledger and does not govern merchandising.

`docs/mapping.md` carries the backend-method table, the pinned submodule commit, the list of read-only SQL fallbacks (currently one: product variants), and the web-shared decision from Task 13.

`docs/install.md` covers Python 3.12, the submodule, and the glibc 2.34 wheel caveat.

`README.md` says what the repo is, how to run the demo, and where the interfaces are — no history, no process narrative.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/python -m pytest -v && .venv/bin/python scripts/check.py`
Expected: everything passes.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/ scripts/ LICENSE tests/
git commit -m "Document the enforcement boundary and demonstrate three denials"
```

---

## Self-Review

**Spec coverage.** §3 layout → Tasks 1, 11, 12, 13. §3 submodule → Task 1. §4 adapter and mapping → Tasks 2, 5, 6, 9, 10. §4 search gap → Task 4. §4 policy/disclosure gap → Task 6. §4 campaign gap → Task 10. §4 catalog gaps (merchandising object, unbound `get_variants`) → Task 3. §4 id convention → Tasks 3, 5. §5 governed commands and the doubled rule → Tasks 7, 10, 11. §5 three denials → Task 14. §6 staging → Task 8. §7 host → Task 11. §7 MCP → Task 12. §7 web → Task 13. §8 testing → every task, plus Task 14 for `check.py`. §9 constraints → Global Constraints and Task 1. §10 success criteria → Tasks 6 and 10 (no abstract methods), 11 (persisted effect), 14 (denials, docs, one install).

**Placeholders.** The one deliberately unfinished artifact is `_CATALOG` in Task 3, whose five remaining entries are specified by shape and by the properties later tests depend on (a zero-stock SKU, a sub-4.0 rating). Task 12's `list_tools()` accessor and Task 3's `product_variants` column names are explicitly marked "the code is the authority, correct it there" rather than guessed at — those are the two places this plan could be wrong about an upstream detail, and both say so.

**Type consistency.** `EngineStore.call` / `.write` / `.readonly_sql` / `.bind` / `.binding` are used as defined in Tasks 3–12. `Merchandising` field names in Task 3's seed match its model. `Receipt.ok` and `KernelClient.execute(...)` in Task 7 match their uses in Tasks 10 and 11. `EngineMerchant.approve` in Task 10 matches Task 11's approval route. `SKILLS_DIR(role)` in Task 1 matches Tasks 11 and 12.
