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
    tent_product_id = next(
        p.id for p in store.commerce.products.list() if p.name == "Ridgeline 2-Person Tent"
    )
    assert first.product_id == tent_product_id

    variant_details = await backend.get_product_details(session(store), "TENT-RIDGE-GRN")
    assert variant_details.product_id == "TENT-RIDGE-GRN"


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
