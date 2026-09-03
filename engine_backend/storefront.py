"""``StorefrontBackend`` over the engine.

The id convention: a purchasable record's ``product_id`` is the engine variant SKU; a
family's ``product_id`` is the engine product id. The engine's cart item exposes only
``sku``, so SKU is the only key that makes ``update_cart_item`` and ``remove_from_cart``
resolvable, and it keeps provenance ids stable across turns for upstream's cart gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from shopping_agent.backend import StorefrontBackend
from shopping_agent.types import (
    Cart,
    CartItem,
    CheckoutHandoff,
    Disclosure,
    FulfillmentOption,
    Order,
    OrderItem,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    UserPreferences,
)
from stateset_embedded import AddCartItemInput, Commerce, ProductVariant
from stateset_embedded import Order as EngineOrder
from stateset_embedded import Product as EngineProduct

from engine_backend import content, money
from engine_backend.catalog import (
    CatalogRow,
    Merchandising,
    catalog_rows,
    list_variants,
    read_merchandising,
)
from engine_backend.listings import (
    FamilyResolution,
    ListingShape,
    VariantResolution,
    family_shape,
    resolve_family_or_variant,
)
from engine_backend.listings import shape as _shape_variant
from engine_backend.search import search as engine_search
from engine_backend.store import EngineStore

_STATUS_MAP: dict[str, OrderStatus] = {
    "pending": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "paid": OrderStatus.PROCESSING,
    "shipped": OrderStatus.SHIPPED,
    "out_for_delivery": OrderStatus.OUT_FOR_DELIVERY,
    "delivered": OrderStatus.DELIVERED,
    "delayed": OrderStatus.DELAYED,
    "cancelled": OrderStatus.CANCELLED,
    "canceled": OrderStatus.CANCELLED,
    "return_initiated": OrderStatus.RETURN_INITIATED,
    "refunded": OrderStatus.REFUNDED,
}


def _to_order(order: EngineOrder) -> Order:
    tracking_url = (
        f"https://track.acme-supply.example/{order.tracking_number}"
        if order.tracking_number
        else None
    )
    return Order(
        order_id=order.id,
        status=_STATUS_MAP.get(order.status.lower(), OrderStatus.PROCESSING),
        placed_at=datetime.fromisoformat(order.created_at.replace("Z", "+00:00")),
        items=[
            OrderItem(
                product_id=item.sku,
                title=item.name,
                quantity=item.quantity,
                price=money.to_float(item.unit_price_exact),
            )
            for item in order.items
        ],
        total=money.to_float(order.total_amount_exact),
        currency=order.currency,
        tracking_url=tracking_url,
    )


def _to_product(listing_shape: ListingShape) -> Product:
    return Product(
        product_id=listing_shape.id,
        title=listing_shape.title,
        brand=listing_shape.merch.brand,
        price=listing_shape.price,
        rating=listing_shape.merch.rating,
        review_count=listing_shape.merch.review_count,
        image_url=listing_shape.merch.image_url,
        category=listing_shape.merch.category,
        labels=list(listing_shape.merch.labels),
        attributes=dict(listing_shape.merch.attributes),
        in_stock=listing_shape.in_stock,
        short_description=listing_shape.short_description,
        option_values=dict(listing_shape.option_values),
        options=dict(listing_shape.options),
        variant_of=listing_shape.variant_of,
    )


def to_product(
    row: CatalogRow, variant_count: int | None = None, variant_of: str | None = None
) -> Product:
    """A purchasable record for one variant: ``product_id`` is the SKU."""
    count = variant_count if variant_count is not None else 1
    return _to_product(_shape_variant(row, variant_count=count, variant_of=variant_of))


def to_family(
    product: EngineProduct,
    variants: list[ProductVariant],
    merch: Merchandising,
    rows: list[CatalogRow],
) -> Product:
    """The family record: ``product_id`` is the engine product id."""
    return _to_product(family_shape(product, variants, merch, rows))


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
        # One catalog scan for the whole result set, not one per family result.
        every_row: list[CatalogRow] | None = None
        for row in rows:
            variants = await list_variants(self.store, row.product.id)
            if len(variants) > 1:
                if every_row is None:
                    every_row = await catalog_rows(self.store)
                all_rows = [r for r in every_row if r.product.id == row.product.id]
                results.append(to_family(row.product, variants, row.merch, all_rows))
            else:
                results.append(to_product(row, variant_count=1))
        return results

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        resolution = await resolve_family_or_variant(self.store, product_id)

        if isinstance(resolution, FamilyResolution):
            family = _to_product(
                family_shape(
                    resolution.product, resolution.variants, resolution.merch, resolution.rows
                )
            )
            variant_products = [
                to_product(r, variant_count=len(resolution.rows), variant_of=resolution.product.id)
                for r in resolution.rows
            ]
            return ProductDetails(
                **family.model_dump(),
                long_description=resolution.merch.long_description,
                specs=dict(resolution.merch.specs),
                variants=variant_products,
            )

        if isinstance(resolution, VariantResolution):
            product = to_product(
                resolution.row,
                variant_count=resolution.variant_count,
                variant_of=resolution.row.product.id,
            )
            return ProductDetails(
                **product.model_dump(),
                long_description=resolution.row.merch.long_description,
                specs=dict(resolution.row.merch.specs),
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

        skus = {item.sku for item in items}

        def resolve_variants(c: Commerce) -> dict[str, ProductVariant | None]:
            return {sku: c.products.get_variant_by_sku(sku) for sku in skus}

        variants_by_sku = await self.store.call(resolve_variants) if skus else {}

        product_ids = {v.product_id for v in variants_by_sku.values() if v is not None}
        merch_by_product = {
            product_id: await read_merchandising(self.store, product_id)
            for product_id in product_ids
        }

        cart_items: list[CartItem] = []
        for item in items:
            variant = variants_by_sku.get(item.sku)
            merch = merch_by_product.get(variant.product_id) if variant is not None else None
            merch = merch if merch is not None else Merchandising()
            option_values = merch.variant_options.get(item.sku, {}) if variant is not None else {}
            cart_items.append(
                CartItem(
                    product_id=item.sku,
                    title=item.name,
                    price=money.to_float(item.unit_price_exact),
                    quantity=item.quantity,
                    option_values=dict(option_values),
                    variant_of=variant.product_id if variant is not None else None,
                )
            )
        return Cart(items=cart_items)

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        cart_id = await self._cart_id(session)
        return await self._to_cart(cart_id)

    def session_cart_id(self, session_id: str) -> str | None:
        """Host-only, not part of :class:`StorefrontBackend`: the id of the cart *this
        session* created, or ``None`` when it has not created one yet.

        The host's checkout route needs the session's own cart, not the customer's
        latest one — every shopping session in this demo binds to the same seeded
        customer, so ``carts.for_customer(...)[-1]`` would let two concurrent sessions
        check out each other's cart. This mapping is the only record of which cart
        belongs to which session, and it is server-held, never a request or tool
        argument."""
        return self._cart_ids.get(session_id)

    async def cart_exact_totals(self, session: ShoppingSessionContext) -> dict[str, Any]:
        """Host-only, not part of :class:`StorefrontBackend`: the engine's own exact
        decimal totals for this cart, so a host route can hand the browser a figure the
        engine vouched for instead of one recomputed from ``float`` prices. Keyed by
        ``product_id`` (the engine's SKU, matching :meth:`_to_cart`'s cart items) rather
        than returned as a ``Cart``, since ``shopping_agent.types.Cart`` has no field for
        it."""
        cart_id = await self._cart_id(session)
        engine_cart = await self.store.call(lambda c: c.carts.get(cart_id))
        items = await self.store.call(lambda c: c.carts.get_items(cart_id))
        return {
            "subtotal_exact": engine_cart.subtotal_exact if engine_cart else None,
            "grand_total_exact": engine_cart.grand_total_exact if engine_cart else None,
            "line_totals_exact": {item.sku: item.total_exact for item in items},
        }

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
        binding = self.store.binding(session.session_id)
        orders = await self.store.call(lambda c: c.orders.list())
        mine = [o for o in orders if o.customer_id == binding.subject_id]
        mine.sort(key=lambda o: o.created_at, reverse=True)
        return [_to_order(o) for o in mine[:limit]]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        binding = self.store.binding(session.session_id)
        order = await self.store.call(lambda c: c.orders.get(order_id))
        if order is None or order.customer_id != binding.subject_id:
            return None
        return _to_order(order)

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        return await content.find_policies(self.store, query)

    async def get_disclosure(
        self, session: ShoppingSessionContext, product_id: str
    ) -> Disclosure | None:
        return await content.find_disclosure(self.store, product_id)

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        # The engine has no per-item shipping quote, so ``product_ids`` is not used:
        # rates come from the session's own cart, and a session with no cart yet gets
        # an empty list.
        cart_id = self._cart_ids.get(session.session_id)
        if cart_id is None:
            return []

        rates = await self.store.call(lambda c: c.carts.get_shipping_rates(cart_id))
        return [
            FulfillmentOption(
                method="shipping",
                eta=(
                    f"{rate.estimated_days} business day{'s' if rate.estimated_days != 1 else ''}"
                    if rate.estimated_days is not None
                    else (rate.description or rate.service)
                ),
                fee=money.to_float(str(rate.price)),
                location=f"{rate.carrier} {rate.service}",
            )
            for rate in rates
        ]

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        if self.checkout_base_url is None:
            return []
        cart_id = await self._cart_id(session)
        return [CheckoutHandoff(url=f"{self.checkout_base_url}?cart={cart_id}")]
