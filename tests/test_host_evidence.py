"""`host/app.py` attaches a structured `evidence` field to the `change_update` event, read
from the persisted staged-change record (`engine_backend.staging.load_evidence`) -- never
parsed out of `guardrail_notes` prose.

These tests run both real apply paths -- the ungoverned one (activity log) and the
governed one (sealed kernel receipt) -- and assert the structured field, not note text,
is what the host and portal see. They also pin down that no regex remains anywhere in
that path.
"""

import inspect

from merchant_agent.types import InventoryActionItem, MerchantSessionContext, PriceUpdateItem
from stateset_embedded import CreateProductVariantInput

import host.app as host_app
from engine_backend.merchant import EngineMerchant
from engine_backend.staging import load_evidence


def session():
    return MerchantSessionContext(
        session_id="m-1", merchant_id="acme", operator="user:acme-operator"
    )


async def _apply(backend, change):
    backend.approve(change.change_id, "user:acme-operator")
    return await backend.apply_change(session(), change.change_id)


async def test_an_ungoverned_apply_persists_structured_activity_log_evidence(store, kernel):
    backend = EngineMerchant(store, kernel)
    applied = await _apply(
        backend,
        await backend.stage_price_update(
            session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        ),
    )
    evidence = await load_evidence(store, applied.change_id)
    assert evidence, f"no structured evidence persisted for {applied.change_id!r}"
    assert [e.kind for e in evidence] == ["activity_log"]
    assert evidence[0].id


async def test_a_governed_apply_persists_structured_kernel_receipt_evidence(store, kernel):
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
    evidence = await load_evidence(store, applied.change_id)
    assert evidence, f"no structured evidence persisted for {applied.change_id!r}"
    assert [e.kind for e in evidence] == ["kernel_receipt"]
    assert evidence[0].id


async def test_the_host_change_update_event_carries_the_structured_field_verbatim(store, kernel):
    backend = EngineMerchant(store, kernel)
    applied = await _apply(
        backend,
        await backend.stage_price_update(
            session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        ),
    )
    from commerce_common.streaming import AgentEvent

    event = AgentEvent(type="change_update", data={"change": applied.model_dump(mode="json")})
    attached = await host_app._with_change_evidence(store, event)
    evidence = attached.data["change"]["evidence"]
    assert [e["kind"] for e in evidence] == ["activity_log"]
    assert evidence[0]["id"]


def test_no_regex_remains_anywhere_in_the_evidence_path():
    """The old coupling was a `re.compile` pinned to `merchant.py`'s wording. Assert
    outright that `host/app.py` no longer imports or uses `re` at all.

    This is a source-text check, not a guarantee: `from re import search as _s` (or any
    other alias/indirection) would slip past it. It catches the coupling this module
    actually had, not every conceivable way to reintroduce a regex."""
    source = inspect.getsource(host_app)
    assert "import re" not in source
    assert "re.compile" not in source
    assert "re.search" not in source


async def test_a_change_update_without_a_change_id_passes_through_unchanged(store, kernel):
    """`_with_change_evidence` runs inside an SSE generator, so a `KeyError` on a change
    dict missing `change_id` breaks the stream rather than dropping one field."""
    from commerce_common.streaming import AgentEvent

    event = AgentEvent(type="change_update", data={"change": {"status": "applied"}})
    assert await host_app._with_change_evidence(store, event) is event
    assert await host_app._with_change_evidence(store, AgentEvent(type="change_update", data={}))


async def test_a_staged_change_update_costs_no_evidence_read(store, kernel, monkeypatch):
    """Evidence exists only for an applied change, so the read is skipped for a staged
    or discarded one instead of spending a database round trip per event to learn it."""
    from commerce_common.streaming import AgentEvent

    backend = EngineMerchant(store, kernel)
    staged = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )

    async def fail(*args, **kwargs):
        raise AssertionError("load_evidence was called for a change that cannot have any")

    monkeypatch.setattr(host_app, "load_evidence", fail)
    event = AgentEvent(type="change_update", data={"change": staged.model_dump(mode="json")})
    assert await host_app._with_change_evidence(store, event) is event
