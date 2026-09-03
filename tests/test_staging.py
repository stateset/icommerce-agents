from merchant_agent.types import ChangeItem, ChangeKind, ChangeStatus

from engine_backend.staging import load, new_change, pending, save


async def test_a_staged_change_round_trips(store):
    change = new_change(
        kind=ChangeKind.PRICE_UPDATE,
        summary="Drop the tan tent to 199.00",
        items=[],
        operator="user:acme-operator",
    )
    await save(store, change)
    loaded = await load(store, change.change_id)
    assert loaded is not None
    assert loaded.summary == change.summary
    assert loaded.status is ChangeStatus.STAGED
    assert loaded.created_by == "user:acme-operator"


async def test_pending_excludes_applied_and_discarded(store):
    staged = new_change(ChangeKind.PRICE_UPDATE, "staged", [], "user:acme-operator")
    applied = new_change(ChangeKind.PRICE_UPDATE, "applied", [], "user:acme-operator")
    applied.status = ChangeStatus.APPLIED
    await save(store, staged)
    await save(store, applied)
    assert [c.summary for c in await pending(store)] == ["staged"]


async def test_load_of_an_unknown_id_is_none(store):
    assert await load(store, "chg-nope") is None


async def test_a_fully_populated_change_round_trips_by_full_equality(store):
    change = new_change(
        kind=ChangeKind.PRICE_UPDATE,
        summary="Drop the tan tent to 199.00",
        items=[ChangeItem(target="listing:tent-tan", field="price", before=249.0, after=199.0)],
        operator="user:acme-operator",
        currency="USD",
        guardrail_notes=["Margin drops from 42% to 31%, above the 25% floor."],
    )
    change.margin_impact = -0.11
    change.margin_before_pct = 42.0
    change.margin_after_pct = 31.0
    await save(store, change)
    loaded = await load(store, change.change_id)
    assert loaded == change


async def test_pending_and_load_on_a_store_with_no_staged_changes(store):
    assert await pending(store) == []
    assert await load(store, "chg-nope") is None
