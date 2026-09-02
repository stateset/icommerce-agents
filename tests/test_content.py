from engine_backend.content import find_disclosure, find_policies, seed_content


async def test_policies_are_searchable(store):
    seed_content(store.commerce)
    hits = await find_policies(store, "return")
    assert hits and "return" in hits[0].title.lower()
    assert await find_policies(store, "cryptocurrency") == []


async def test_a_disclosure_is_server_authored(store):
    seed_content(store.commerce)
    disclosure = await find_disclosure(store, "TENT-RIDGE-GRN")
    assert disclosure is not None
    assert disclosure.rows
