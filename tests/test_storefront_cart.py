import asyncio

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


async def test_concurrent_first_writes_share_one_session_cart(store):
    """The executor deliberately permits parallel tool calls in one turn. Two first
    writes must not both pass the empty cart-id cache and create separate carts."""
    backend = EngineStorefront(store)
    ctx = session(store)

    await asyncio.gather(
        backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 1),
        backend.add_to_cart(ctx, "LAMP-BEACON-BLK", 1),
    )

    assert len(store.commerce.carts.list()) == 1
    cart = await backend.get_cart(ctx)
    assert {item.product_id for item in cart.items} == {
        "TENT-RIDGE-GRN",
        "LAMP-BEACON-BLK",
    }


async def test_backend_enforces_the_per_item_cap_defensively(store):
    """Agent gates normally reduce the quantity before this boundary, but direct host
    routes and future callers must not be able to bypass the invariant."""
    backend = EngineStorefront(store, max_quantity_per_item=24)
    ctx = session(store)

    await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 20)
    cart = await backend.add_to_cart(ctx, "TENT-RIDGE-GRN", 20)

    assert cart.items[0].quantity == 24


async def test_get_cart_reads_are_bounded_not_per_line(store):
    """_to_cart must not issue a store.call per cart line: the number of round trips
    through the thread pool should stay flat as lines are added, not grow with them."""
    backend = EngineStorefront(store)
    ctx = session(store)

    skus = [
        "TENT-RIDGE-GRN",
        "TENT-RIDGE-TAN",
        "BAG-SUMMIT-REG",
        "BAG-SUMMIT-LNG",
        "PACK-SWITCH-SLT",
    ]
    for sku in skus:
        await backend.add_to_cart(ctx, sku, 1)

    original_call = store.call
    calls = {"count": 0}

    async def counting_call(fn):
        calls["count"] += 1
        return await original_call(fn)

    store.call = counting_call
    try:
        cart = await backend.get_cart(ctx)
    finally:
        store.call = original_call

    # One call for get_items, one for the batched variant lookup, plus one
    # read_merchandising per distinct *product family* (3 here: tent, bag, pack) —
    # flat regardless of line count. The old N+1 code made 2 calls per line (10
    # for 5 lines); this pins it well below that.
    assert calls["count"] == 5
    assert calls["count"] < 2 * len(skus)

    assert sorted((i.product_id, i.quantity) for i in cart.items) == sorted(
        (sku, 1) for sku in skus
    )
    grn = next(i for i in cart.items if i.product_id == "TENT-RIDGE-GRN")
    assert grn.option_values == {"colour": "green"}
    assert grn.variant_of is not None
    reg = next(i for i in cart.items if i.product_id == "BAG-SUMMIT-REG")
    assert reg.option_values == {"length": "regular"}
