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
