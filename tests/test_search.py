from shopping_agent.types import SearchFilters

from engine_backend.catalog import catalog_rows, write_merchandising
from engine_backend.search import search


async def test_matches_on_title_and_respects_limit(store):
    rows = await search(store, "tent", None, limit=8)
    assert rows and rows[0].product.name == "Ridgeline 2-Person Tent"
    assert len(await search(store, "tent", None, limit=1)) == 1


async def test_a_family_is_one_result(store):
    rows = await search(store, "ridgeline", None, limit=8)
    matches = [r for r in rows if r.product.name == "Ridgeline 2-Person Tent"]
    assert len(matches) == 1
    # The family is represented by its in-stock, cheapest variant: both tent variants
    # are $219.00, so the tie-break must not silently pick an out-of-stock or pricier one.
    assert matches[0].variant.sku == "TENT-RIDGE-GRN"
    assert matches[0].stock > 0


async def test_filters_narrow_by_category_and_price(store):
    rows = await search(store, "", SearchFilters(category="camping", max_price=100.0), limit=8)
    assert rows
    assert all(r.merch.category == "camping" for r in rows)
    assert all(r.variant.price <= 100.0 for r in rows)


async def test_filters_narrow_by_min_price(store):
    rows = await search(store, "", SearchFilters(min_price=150.0), limit=8)
    assert rows
    assert all(r.variant.price >= 150.0 for r in rows)


async def test_filters_narrow_by_attributes(store):
    rows = await search(store, "", SearchFilters(attributes={"capacity": "22L"}), limit=8)
    assert rows
    assert all(r.merch.attributes.get("capacity") == "22L" for r in rows)


async def test_sort_by_price_ascending(store):
    rows = await search(store, "", SearchFilters(sort="price_asc"), limit=8)
    prices = [r.variant.price for r in rows]
    assert prices == sorted(prices)


async def test_sort_by_rating(store):
    rows = await search(store, "", SearchFilters(sort="rating"), limit=8)
    ratings = [r.merch.rating or 0.0 for r in rows]
    assert ratings == sorted(ratings, reverse=True)


async def test_empty_query_orders_by_price_ascending(store):
    rows = await search(store, "", None, limit=8)
    prices = [r.variant.price for r in rows]
    assert prices == sorted(prices)
    assert prices[0] == 45.00
    assert rows[0].product.name == "Beacon 300 Headlamp"


async def test_min_rating_excludes_an_unrated_product(store):
    all_rows = await catalog_rows(store)
    target = next(r for r in all_rows if r.product.name == "Beacon 300 Headlamp")
    unrated = target.merch.model_copy(update={"rating": None})
    await write_merchandising(store, target.product.id, unrated)

    with_filter = await search(store, "", SearchFilters(min_rating=4.0), limit=8)
    assert "Beacon 300 Headlamp" not in [r.product.name for r in with_filter]

    without_filter = await search(store, "", None, limit=8)
    assert "Beacon 300 Headlamp" in [r.product.name for r in without_filter]


async def test_no_match_returns_empty(store):
    assert await search(store, "submarine", None, limit=8) == []
