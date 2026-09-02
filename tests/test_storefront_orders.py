from shopping_agent.types import ShoppingSessionContext

from engine_backend.storefront import EngineStorefront


def session(store):
    # The seeded customer with the tent order is looked up by email rather than
    # customers.list()[0]: the engine's list() does not return customers in creation
    # order (it comes back alphabetically by name in this build), so indexing into it
    # does not reliably pick "Rowan Ellis," the customer this test's assertions are
    # written against.
    rowan = store.commerce.customers.get_by_email("rowan@example.invalid")
    store.bind("sess-1", rowan.id, "customer")
    return ShoppingSessionContext(session_id="sess-1", user_id="rowan")


async def test_orders_are_the_sessions_own(store):
    # Rowan Ellis has two seeded orders (the tent and the sleeping bag), newest first.
    orders = await EngineStorefront(store).get_orders(session(store))
    assert len(orders) == 2
    assert orders[0].placed_at >= orders[1].placed_at
    by_sku = {o.items[0].product_id: o for o in orders}
    assert set(by_sku) == {"TENT-RIDGE-GRN", "BAG-SUMMIT-REG"}
    assert by_sku["TENT-RIDGE-GRN"].total == 219.00


async def test_another_customers_order_is_not_returned(store):
    other = store.commerce.customers.create(
        email="stranger@example.invalid", first_name="Sam", last_name="Vale"
    )
    store.bind("sess-2", other.id, "customer")
    ctx = ShoppingSessionContext(session_id="sess-2", user_id="sam")
    backend = EngineStorefront(store)
    mine = next(
        o
        for o in store.commerce.orders.list()
        if o.customer_id == store.commerce.customers.get_by_email("rowan@example.invalid").id
    )
    assert await backend.get_orders(ctx) == []
    assert await backend.get_order(ctx, mine.id) is None


async def test_preferences_come_from_the_bound_customer(store):
    prefs = await EngineStorefront(store).get_preferences(session(store))
    assert prefs.display_name == "Rowan Ellis"


async def test_fulfillment_options_with_no_cart_is_empty(store):
    options = await EngineStorefront(store).get_fulfillment_options(
        session(store), ["TENT-RIDGE-GRN"]
    )
    assert options == []


async def test_fulfillment_options_come_from_the_engine(store):
    backend = EngineStorefront(store)
    ctx = session(store)
    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1)
    options = await backend.get_fulfillment_options(ctx, ["TENT-RIDGE-GRN"])
    # The engine's carts.get_shipping_rates returns rates for any cart regardless of
    # whether a shipping address has been set; with an item in the cart it comes back
    # non-empty, so this asserts the real shape rather than just isinstance(list).
    assert options
    assert all(o.method == "shipping" for o in options)


async def test_checkout_handoff_returns_a_host_url(store):
    backend = EngineStorefront(store, checkout_base_url="http://localhost:8000/checkout")
    ctx = session(store)
    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1)
    cart = await backend.get_cart(ctx)
    handoffs = await backend.checkout_handoff(ctx, cart)
    assert handoffs and handoffs[0].url.startswith("http://localhost:8000/checkout")


def test_no_abstract_methods_remain():
    assert EngineStorefront.__abstractmethods__ == frozenset()
