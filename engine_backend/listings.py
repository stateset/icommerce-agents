"""The resolution and shaping logic shared by ``storefront.py``'s ``Product`` and
``merchant.py``'s ``Listing``: the same family-then-variant lookup, the same
``by_sku``/option-collection loop, and the same title rule. Each side still builds its
own upstream type from the :class:`ListingShape` this module produces -- ``Listing`` has
``stock``, ``content_quality`` and ``status``; ``Product`` has ``rating``,
``review_count`` and ``in_stock`` -- so those fields are not collapsed here, only made
available on the shape for each side to pick from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from merchant_agent.types import Listing
from stateset_embedded import Product, ProductVariant

from engine_backend import money
from engine_backend.catalog import CatalogRow, Merchandising, catalog_rows, list_variants
from engine_backend.store import EngineStore

_STATUSES = {"active", "paused", "draft", "out_of_stock"}


def title(product: Product, variant: ProductVariant, variant_count: int) -> str:
    if variant_count > 1 and variant.name:
        return f"{product.name} ({variant.name})"
    return product.name


@dataclass
class ListingShape:
    """Everything both sides' listing/product records are built from, for one purchasable
    variant or one product family."""

    id: str
    title: str
    product_status: str
    merch: Merchandising
    price: float
    stock: int
    in_stock: bool
    short_description: str | None
    option_values: dict[str, str] = field(default_factory=dict)
    options: dict[str, list[str]] = field(default_factory=dict)
    variant_of: str | None = None


def shape(row: CatalogRow, variant_count: int = 1, variant_of: str | None = None) -> ListingShape:
    """The shape for one purchasable variant: ``id`` is the SKU."""
    stock = int(row.stock)
    return ListingShape(
        id=row.variant.sku,
        title=title(row.product, row.variant, variant_count),
        product_status=row.product.status,
        merch=row.merch,
        price=money.to_float(row.variant.price_exact),
        stock=stock,
        in_stock=stock > 0,
        short_description=row.product.description or None,
        option_values=dict(row.merch.variant_options.get(row.variant.sku, {})),
        variant_of=variant_of,
    )


def family_shape(
    product: Product,
    variants: list[ProductVariant],
    merch: Merchandising,
    rows: list[CatalogRow],
) -> ListingShape:
    """The shape for a product family: ``id`` is the engine product id."""
    by_sku = {row.variant.sku: row for row in rows}
    options: dict[str, list[str]] = {name: [] for name in merch.option_names}
    for variant in variants:
        values = merch.variant_options.get(variant.sku, {})
        for name in merch.option_names:
            value = values.get(name)
            if value is not None and value not in options[name]:
                options[name].append(value)

    prices = [money.to_float(v.price_exact) for v in variants]
    total_stock = sum(int(by_sku[v.sku].stock) for v in variants if v.sku in by_sku)
    any_in_stock = any(by_sku[v.sku].stock > 0 for v in variants if v.sku in by_sku)

    return ListingShape(
        id=product.id,
        title=product.name,
        product_status=product.status,
        merch=merch,
        price=min(prices) if prices else 0.0,
        stock=total_stock,
        in_stock=any_in_stock,
        short_description=product.description or None,
        options=options,
    )


@dataclass
class FamilyResolution:
    product: Product
    merch: Merchandising
    variants: list[ProductVariant]
    rows: list[CatalogRow]


@dataclass
class VariantResolution:
    row: CatalogRow
    variant_count: int


async def resolve_family_or_variant(
    store: EngineStore, listing_id: str
) -> FamilyResolution | VariantResolution | None:
    """``listing_id`` resolved against the catalog: a product family (more than one row
    shares its product id), a single variant, or neither."""
    rows = await catalog_rows(store)

    family_rows = [r for r in rows if r.product.id == listing_id]
    if family_rows:
        product = family_rows[0].product
        merch = family_rows[0].merch
        variants = await list_variants(store, product.id)
        return FamilyResolution(product=product, merch=merch, variants=variants, rows=family_rows)

    variant_row = next((r for r in rows if r.variant.sku == listing_id), None)
    if variant_row is not None:
        variant_count = len([r for r in rows if r.product.id == variant_row.product.id])
        return VariantResolution(row=variant_row, variant_count=variant_count)

    return None


# -- The merchant side's own type, built from the shape above -------------------------


def _content_quality(merch: Merchandising) -> Literal["good", "needs_work", "poor"]:
    has_description = bool(merch.long_description)
    has_specs = bool(merch.specs)
    if has_description and has_specs:
        return "good"
    if has_description or has_specs:
        return "needs_work"
    return "poor"


def _status(
    product_status: str, in_stock: bool
) -> Literal["active", "paused", "draft", "out_of_stock"]:
    if product_status == "active" or product_status not in _STATUSES:
        return "active" if in_stock else "out_of_stock"
    return cast(Literal["active", "paused", "draft", "out_of_stock"], product_status)


def _to_listing(listing_shape: ListingShape) -> Listing:
    return Listing(
        listing_id=listing_shape.id,
        title=listing_shape.title,
        status=_status(listing_shape.product_status, listing_shape.in_stock),
        price=listing_shape.price,
        stock=listing_shape.stock,
        category=listing_shape.merch.category,
        content_quality=_content_quality(listing_shape.merch),
        attributes=dict(listing_shape.merch.attributes),
        image_url=listing_shape.merch.image_url,
        short_description=listing_shape.short_description,
        option_values=dict(listing_shape.option_values),
        options=dict(listing_shape.options),
        variant_of=listing_shape.variant_of,
    )


def to_listing(row: CatalogRow, variant_count: int = 1, variant_of: str | None = None) -> Listing:
    """A plain listing, or one variant of a family (``variant_of`` set)."""
    return _to_listing(shape(row, variant_count=variant_count, variant_of=variant_of))


def family_listing(
    product: Product,
    variants: list[ProductVariant],
    merch: Merchandising,
    rows: list[CatalogRow],
) -> Listing:
    """The family listing: ``listing_id`` is the engine product id."""
    return _to_listing(family_shape(product, variants, merch, rows))
