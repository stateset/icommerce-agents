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
