from shopping_agent.types import SearchFilters

from engine_backend.search import search


async def test_matches_on_title_and_respects_limit(store):
    rows = await search(store, "tent", None, limit=8)
    assert rows and rows[0].product.name == "Ridgeline 2-Person Tent"
    assert len(await search(store, "tent", None, limit=1)) == 1


async def test_a_family_is_one_result(store):
    rows = await search(store, "ridgeline", None, limit=8)
    assert [r.product.name for r in rows].count("Ridgeline 2-Person Tent") == 1


async def test_filters_narrow_by_category_and_price(store):
    rows = await search(store, "", SearchFilters(category="camping", max_price=100.0), limit=8)
    assert rows
    assert all(r.merch.category == "camping" for r in rows)
    assert all(r.variant.price <= 100.0 for r in rows)


async def test_sort_by_price_ascending(store):
    rows = await search(store, "", SearchFilters(sort="price_asc"), limit=8)
    prices = [r.variant.price for r in rows]
    assert prices == sorted(prices)


async def test_no_match_returns_empty(store):
    assert await search(store, "submarine", None, limit=8) == []
