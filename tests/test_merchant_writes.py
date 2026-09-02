import json

import pytest
from merchant_agent.types import ChangeStatus, MerchantSessionContext, PriceUpdateItem

from engine_backend.merchant import EngineMerchant


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def test_staging_a_price_update_changes_nothing_live(store, kernel):
    backend = EngineMerchant(store, kernel)
    before = store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    assert change.status is ChangeStatus.STAGED
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price == before
    assert [c.change_id for c in await backend.get_pending_changes(session())] == [change.change_id]


async def test_apply_without_approval_is_refused(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    from merchant_agent.changes import ChangeNotApplicable

    with pytest.raises(ChangeNotApplicable):
        await backend.apply_change(session(), change.change_id)
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price != 199.00


async def test_an_approved_price_update_writes_and_logs(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    assert applied.applied_by == "user:acme-operator"
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price == 199.00
    # `activity_logs.record` requires a real UUID `subject_id`; the engine has no notion
    # of a `chg-...` id, so the change is referenced by `change_id` in metadata instead.
    logs = store.commerce.activity_logs.list(subject_type="staged_change", limit=10)
    assert any(
        json.loads(entry.metadata or "{}").get("change_id") == change.change_id for entry in logs
    )


async def test_a_restock_of_an_existing_sku_is_not_governed(store, kernel):
    from merchant_agent.types import InventoryActionItem

    backend = EngineMerchant(store, kernel)
    before = store.commerce.inventory.get_stock("TENT-RIDGE-TAN").total_available
    change = await backend.stage_inventory_action(
        session(),
        [InventoryActionItem(listing_id="TENT-RIDGE-TAN", action="restock", quantity=20)],
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    assert store.commerce.inventory.get_stock("TENT-RIDGE-TAN").total_available >= before + 20

    # Evidence for an ungoverned write is an activity-log id, never a receipt.
    assert applied.guardrail_notes
    note = applied.guardrail_notes[-1]
    assert "activity" in note.lower() or "log" in note.lower()
    assert "receipt" not in note.lower()


async def test_a_restock_of_a_new_sku_is_governed(store, kernel):
    from merchant_agent.types import InventoryActionItem
    from stateset_embedded import CreateProductVariantInput

    product = store.commerce.products.create(
        name="Brand New Widget",
        description="A widget with no inventory item yet.",
        variants=[CreateProductVariantInput(sku="WIDGET-NEW-1", price=25.00)],
    )
    assert store.commerce.inventory.get_stock("WIDGET-NEW-1") is None

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_inventory_action(
        session(),
        [InventoryActionItem(listing_id="WIDGET-NEW-1", action="restock", quantity=20)],
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    stock = store.commerce.inventory.get_stock("WIDGET-NEW-1")
    assert stock is not None
    assert stock.total_available >= 20

    # Evidence for a governed write is a sealed kernel receipt id.
    assert applied.guardrail_notes
    note = applied.guardrail_notes[-1]
    assert "receipt" in note.lower()
    del product  # only needed to create the variant


async def test_discard_leaves_live_state_alone(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    discarded = await backend.discard_change(session(), change.change_id)
    assert discarded.status is ChangeStatus.DISCARDED
    assert await backend.get_pending_changes(session()) == []


def test_no_abstract_methods_remain():
    assert EngineMerchant.__abstractmethods__ == frozenset()
