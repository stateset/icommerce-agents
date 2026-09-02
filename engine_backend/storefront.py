"""``StorefrontBackend`` over the engine.

The id convention: a purchasable record's ``product_id`` is the engine variant SKU; a
family's ``product_id`` is the engine product id. The engine's cart item exposes only
``sku``, so SKU is the only key that makes ``update_cart_item`` and ``remove_from_cart``
resolvable, and it keeps provenance ids stable across turns for upstream's cart gate.
"""

from __future__ import annotations

from decimal import Decimal

from shopping_agent.backend import StorefrontBackend
from shopping_agent.types import (
    Cart,
    CartItem,
    Disclosure,
    FulfillmentOption,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    UserPreferences,
)
from stateset_embedded import AddCartItemInput, Commerce, ProductVariant
from stateset_embedded import Product as EngineProduct

from engine_backend.catalog import (
    CatalogRow,
    Merchandising,
    catalog_rows,
    list_variants,
    read_merchandising,
)
from engine_backend.search import search as engine_search
from engine_backend.store import EngineStore


def _title(product: EngineProduct, variant: ProductVariant, variant_count: int) -> str:
    if variant_count > 1 and variant.name:
        return f"{product.name} ({variant.name})"
    return product.name


def to_product(
    row: CatalogRow, variant_count: int | None = None, variant_of: str | None = None
) -> Product:
    """A purchasable record for one variant: ``product_id`` is the SKU."""
    count = variant_count if variant_count is not None else 1
    return Product(
        product_id=row.variant.sku,
        title=_title(row.product, row.variant, count),
        brand=row.merch.brand,
        price=float(Decimal(row.variant.price_exact)),
        rating=row.merch.rating,
        review_count=row.merch.review_count,
        image_url=row.merch.image_url,
        category=row.merch.category,
        labels=list(row.merch.labels),
        attributes=dict(row.merch.attributes),
        in_stock=row.stock > 0,
        short_description=row.product.description or None,
        option_values=dict(row.merch.variant_options.get(row.variant.sku, {})),
        variant_of=variant_of,
    )


def to_family(
    product: EngineProduct,
    variants: list[ProductVariant],
    merch: Merchandising,
    rows: list[CatalogRow],
) -> Product:
    """The family record: ``product_id`` is the engine product id."""
    by_sku = {row.variant.sku: row for row in rows}
    options: dict[str, list[str]] = {name: [] for name in merch.option_names}
    for sku in (v.sku for v in variants):
        values = merch.variant_options.get(sku, {})
        for name in merch.option_names:
            value = values.get(name)
            if value is not None and value not in options[name]:
                options[name].append(value)

    prices = [float(Decimal(v.price_exact)) for v in variants]
    in_stock = any(by_sku[v.sku].stock > 0 for v in variants if v.sku in by_sku)

    return Product(
        product_id=product.id,
        title=product.name,
        brand=merch.brand,
        price=min(prices) if prices else 0.0,
        rating=merch.rating,
        review_count=merch.review_count,
        image_url=merch.image_url,
        category=merch.category,
        labels=list(merch.labels),
        attributes=dict(merch.attributes),
        in_stock=in_stock,
        short_description=product.description or None,
        options=options,
    )


class EngineStorefront(StorefrontBackend):
    def __init__(self, store: EngineStore, checkout_base_url: str | None = None) -> None:
        self.store = store
        self.checkout_base_url = checkout_base_url
        self._cart_ids: dict[str, str] = {}

    # -- Catalog ------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        rows = await engine_search(self.store, query, filters, limit)
        results: list[Product] = []
        for row in rows:
            variants = await list_variants(self.store, row.product.id)
            if len(variants) > 1:
                all_rows = [
                    r for r in await catalog_rows(self.store) if r.product.id == row.product.id
                ]
                results.append(to_family(row.product, variants, row.merch, all_rows))
            else:
                results.append(to_product(row, variant_count=1))
        return results

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        rows = await catalog_rows(self.store)

        family_rows = [r for r in rows if r.product.id == product_id]
        if family_rows:
            product = family_rows[0].product
            merch = family_rows[0].merch
            variants = await list_variants(self.store, product.id)
            family = to_family(product, variants, merch, family_rows)
            variant_products = [
                to_product(r, variant_count=len(family_rows), variant_of=product.id)
                for r in family_rows
            ]
            return ProductDetails(
                **family.model_dump(),
                long_description=merch.long_description,
                specs=dict(merch.specs),
                variants=variant_products,
            )

        variant_row = next((r for r in rows if r.variant.sku == product_id), None)
        if variant_row is not None:
            variant_count = len([r for r in rows if r.product.id == variant_row.product.id])
            product = to_product(
                variant_row, variant_count=variant_count, variant_of=variant_row.product.id
            )
            return ProductDetails(
                **product.model_dump(),
                long_description=variant_row.merch.long_description,
                specs=dict(variant_row.merch.specs),
                variants=[],
            )

        return None

    # -- Cart ---------------------------------------------------------------------

    async def _cart_id(self, session: ShoppingSessionContext) -> str:
        cart_id = self._cart_ids.get(session.session_id)
        if cart_id is not None:
            return cart_id

        binding = self.store.binding(session.session_id)

        def body(c: Commerce) -> str:
            cart = c.carts.create(customer_id=binding.subject_id, currency="USD")
            return cart.id

        cart_id = await self.store.write(session.session_id, body)
        self._cart_ids[session.session_id] = cart_id
        return cart_id

    async def _to_cart(self, cart_id: str) -> Cart:
        items = await self.store.call(lambda c: c.carts.get_items(cart_id))
        cart_items: list[CartItem] = []
        for item in items:
            variant = await self.store.call(lambda c, s=item.sku: c.products.get_variant_by_sku(s))
            merch = (
                await read_merchandising(self.store, variant.product_id)
                if variant is not None
                else Merchandising()
            )
            option_values = merch.variant_options.get(item.sku, {}) if variant is not None else {}
            cart_items.append(
                CartItem(
                    product_id=item.sku,
                    title=item.name,
                    price=float(Decimal(item.unit_price_exact)),
                    quantity=item.quantity,
                    option_values=dict(option_values),
                    variant_of=variant.product_id if variant is not None else None,
                )
            )
        return Cart(items=cart_items)

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        cart_id = await self._cart_id(session)
        return await self._to_cart(cart_id)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        cart_id = await self._cart_id(session)

        def body(c: Commerce) -> None:
            variant = c.products.get_variant_by_sku(product_id)
            if variant is None:
                return
            existing = next((i for i in c.carts.get_items(cart_id) if i.sku == product_id), None)
            if existing is not None:
                c.carts.update_item(existing.id, quantity=existing.quantity + quantity)
                return
            c.carts.add_item(
                cart_id,
                AddCartItemInput(
                    sku=variant.sku,
                    name=variant.name or variant.sku,
                    quantity=quantity,
                    unit_price=variant.price,
                    product_id=variant.product_id,
                    variant_id=variant.id,
                ),
            )

        await self.store.write(session.session_id, body)
        return await self._to_cart(cart_id)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        cart_id = await self._cart_id(session)

        def body(c: Commerce) -> None:
            existing = next((i for i in c.carts.get_items(cart_id) if i.sku == product_id), None)
            if existing is None:
                return
            c.carts.update_item(existing.id, quantity=quantity)

        await self.store.write(session.session_id, body)
        return await self._to_cart(cart_id)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        cart_id = await self._cart_id(session)

        def body(c: Commerce) -> None:
            existing = next((i for i in c.carts.get_items(cart_id) if i.sku == product_id), None)
            if existing is None:
                return
            c.carts.remove_item(existing.id)

        await self.store.write(session.session_id, body)
        return await self._to_cart(cart_id)

    # -- Customer context ---------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        binding = self.store.binding(session.session_id)
        customer = await self.store.call(lambda c: c.customers.get(binding.subject_id))
        display_name = None
        if customer is not None:
            display_name = (
                " ".join(part for part in (customer.first_name, customer.last_name) if part) or None
            )
        return UserPreferences(user_id=session.user_id, display_name=display_name)

    # -- Orders and policies --------------------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        raise NotImplementedError

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        raise NotImplementedError

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        raise NotImplementedError

    async def get_disclosure(
        self, session: ShoppingSessionContext, product_id: str
    ) -> Disclosure | None:
        return None

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        raise NotImplementedError
