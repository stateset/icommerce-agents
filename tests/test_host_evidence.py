"""`host/app.py` turns the free-text evidence `EngineMerchant._apply_write` records into
a structured `evidence` field on the `change_update` event, by regex.

Nothing else guards that coupling: reword a note in `merchant.py` and the evidence
silently disappears from the portal with every other test still green. These tests run
both real apply paths -- the ungoverned one (activity log) and the governed one (sealed
kernel receipt) -- and assert the host's own parser recognises what they emit.
"""

import pytest
from merchant_agent.types import InventoryActionItem, MerchantSessionContext, PriceUpdateItem
from stateset_embedded import CreateProductVariantInput

from engine_backend.merchant import EngineMerchant
from host.app import _change_evidence


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def _apply(backend, change):
    backend.approve(change.change_id, "user:acme-operator")
    return await backend.apply_change(session(), change.change_id)


async def test_an_ungoverned_apply_produces_notes_the_host_parses_as_an_activity_log(store, kernel):
    backend = EngineMerchant(store, kernel)
    applied = await _apply(
        backend,
        await backend.stage_price_update(
            session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        ),
    )
    evidence = _change_evidence(applied.guardrail_notes)
    assert evidence, f"the host parsed no evidence out of {applied.guardrail_notes!r}"
    assert [e["kind"] for e in evidence] == ["activity_log"]
    assert evidence[0]["id"]


async def test_a_governed_apply_produces_notes_the_host_parses_as_a_kernel_receipt(store, kernel):
    store.commerce.products.create(
        name="Brand New Widget",
        description="A widget with no inventory item yet.",
        variants=[CreateProductVariantInput(sku="WIDGET-NEW-1", price=25.00)],
    )
    backend = EngineMerchant(store, kernel)
    applied = await _apply(
        backend,
        await backend.stage_inventory_action(
            session(),
            [InventoryActionItem(listing_id="WIDGET-NEW-1", action="restock", quantity=20)],
        ),
    )
    evidence = _change_evidence(applied.guardrail_notes)
    assert evidence, f"the host parsed no evidence out of {applied.guardrail_notes!r}"
    assert [e["kind"] for e in evidence] == ["kernel_receipt"]
    assert evidence[0]["id"]


@pytest.mark.parametrize(
    "note", ["applied via direct binding write; activity log abc-123", "sealed receipt rcpt-9"]
)
def test_the_parser_keeps_the_whole_note_alongside_the_id(note):
    (entry,) = _change_evidence([note])
    assert entry["note"] == note
    assert entry["id"] in note
