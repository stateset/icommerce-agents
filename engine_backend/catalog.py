"""What the engine's catalog does not model, and the one read its binding does not expose.

Merchandising fields (brand, category, imagery, ratings, option values, unit cost) live in
one custom object per product, owned by the product. Variants are read with a single
parameterized SELECT on the read-only connection, because Commerce::get_variants exists in
the Rust crate but is not bound in Python 1.28.5. docs/mapping.md lists that read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

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
            commerce.custom_objects.create_type(handle=handle, display_name=display, fields=fields)


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
                    # A quantity, not money: it never reaches an engine money column,
                    # so it does not go through engine_backend.money.
                    stock=float(stock.total_available) if stock else 0.0,
                )
            )
    return rows
