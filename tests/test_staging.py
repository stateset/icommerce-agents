from merchant_agent.types import ChangeKind, ChangeStatus

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
