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


async def test_save_without_payload_preserves_the_stored_payload(store):
    """`save` must tell "the caller didn't mention `payload`" apart from "the caller
    wants it cleared" -- a bare `None` default would silently erase a promotion or
    campaign draft on every later save that only reports evidence."""
    change = new_change(ChangeKind.PROMOTION, "promo", [], "user:acme-operator")
    await save(store, change, payload={"name": "Autumn Sale", "discount_pct": 15})

    from engine_backend.staging import load_change_payload

    applied = change.model_copy(update={"status": ChangeStatus.APPLIED})
    await save(store, applied)  # no `payload=` kwarg, as apply_change's own save does

    assert await load_change_payload(store, change.change_id) == {
        "name": "Autumn Sale",
        "discount_pct": 15,
    }


async def test_applying_a_promotion_leaves_its_draft_payload_intact(store, kernel):
    """The regression this guards: `EngineMerchant.apply_change` loads the draft, hands
    it to `apply.apply_change`, and re-saves reporting only the resulting evidence --
    never re-passing `payload=`. If `save` treated a missing `payload` as "clear it",
    the draft this promotion was built from would vanish the moment it was applied."""
    from merchant_agent.types import MerchantSessionContext, PromotionDraft

    from engine_backend.merchant import EngineMerchant
    from engine_backend.staging import load_change_payload

    session = MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_promotion(
        session,
        PromotionDraft(
            name="Autumn Sale",
            listing_ids=["TENT-RIDGE-TAN"],
            discount_pct=15,
            starts="2026-09-01",
            ends="2026-09-30",
        ),
    )
    before = await load_change_payload(store, change.change_id)
    assert before is not None

    backend.approve(change.change_id, "user:acme-operator")
    await backend.apply_change(session, change.change_id)

    after = await load_change_payload(store, change.change_id)
    assert after == before, "applying the promotion must not erase its staged draft"
