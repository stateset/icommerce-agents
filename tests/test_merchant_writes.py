import asyncio
import json
import sqlite3

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


async def test_concurrent_apply_executes_an_approved_change_once(store, kernel):
    """Approval is single-use and the status transition surrounds the live mutation.
    Two callers racing the same id must produce one apply and one refusal, not two
    writes and two audit entries."""
    from merchant_agent.changes import ChangeNotApplicable

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:acme-operator")

    results = await asyncio.gather(
        backend.apply_change(session(), change.change_id),
        backend.apply_change(session(), change.change_id),
        return_exceptions=True,
    )

    assert sum(getattr(result, "status", None) is ChangeStatus.APPLIED for result in results) == 1
    assert sum(isinstance(result, ChangeNotApplicable) for result in results) == 1
    logs = store.commerce.activity_logs.list(subject_type="staged_change", limit=10)
    matching = [
        entry
        for entry in logs
        if json.loads(entry.metadata or "{}").get("change_id") == change.change_id
    ]
    assert len(matching) == 1


async def test_approval_is_bound_to_the_operator_and_consumed(store, kernel):
    from merchant_agent.changes import ChangeNotApplicable

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:approver-a")
    other = session().model_copy(update={"operator": "user:approver-b"})

    with pytest.raises(ChangeNotApplicable, match="different operator"):
        await backend.apply_change(other, change.change_id)
    assert change.change_id in backend.approved_ids

    approver = session().model_copy(update={"operator": "user:approver-a"})
    applied = await backend.apply_change(approver, change.change_id)
    assert applied.status is ChangeStatus.APPLIED
    assert change.change_id not in backend.approved_ids


async def test_a_refused_apply_consumes_its_approval(store, kernel):
    from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:acme-operator")
    backend.config = backend.config.model_copy(update={"max_price_delta_pct": 1.0})

    with pytest.raises(GuardrailViolation):
        await backend.apply_change(session(), change.change_id)
    assert change.change_id not in backend.approved_ids
    assert store.approval_record(change.change_id)["state"] == "failed"
    assert "GuardrailViolation" in store.approval_record(change.change_id)["last_error"]

    backend.config = backend.config.model_copy(update={"max_price_delta_pct": 20.0})
    with pytest.raises(ChangeNotApplicable, match="has not been approved"):
        await backend.apply_change(session(), change.change_id)


async def test_failure_after_mutation_starts_requires_reconciliation(store, kernel, monkeypatch):
    from merchant_agent.changes import ChangeNotApplicable

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    backend.approve(change.change_id, "user:acme-operator")

    async def ambiguous_failure(*_args, **_kwargs):
        raise RuntimeError("simulated failure after mutation dispatch")

    monkeypatch.setattr("engine_backend.merchant._apply_change", ambiguous_failure)
    with pytest.raises(RuntimeError, match="simulated failure"):
        await backend.apply_change(session(), change.change_id)

    record = store.approval_record(change.change_id)
    assert record["state"] == "reconciliation_required"
    assert "RuntimeError" in record["last_error"]
    with pytest.raises(ChangeNotApplicable, match="reconciliation"):
        backend.approve(change.change_id, "user:acme-operator")
    with pytest.raises(ChangeNotApplicable, match="reconciliation"):
        await backend.apply_change(session(), change.change_id)

    competing = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=189.00)]
    )
    backend.approve(competing.change_id, "user:acme-operator")
    with pytest.raises(ChangeNotApplicable, match="already being changed"):
        await backend.apply_change(session(), competing.change_id)


async def test_durable_approval_survives_backend_recreation(tmp_path):
    from pathlib import Path

    from engine_backend.kernel import KernelClient
    from engine_backend.seed import seed_store
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "durable.db")
    first_store = EngineStore(db_path)
    seed_store(first_store.commerce)
    first = EngineMerchant(
        first_store,
        KernelClient(
            first_store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    change = await first.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    first.approve(change.change_id, "user:acme-operator")

    second_store = EngineStore(db_path)
    second = EngineMerchant(
        second_store,
        KernelClient(
            second_store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    assert change.change_id in second.approved_ids
    applied = await second.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    assert second_store.approval_record(change.change_id)["state"] == "applied"
    assert first_store.approval_record(change.change_id)["state"] == "applied"


async def test_stale_approved_price_change_cannot_overwrite_newer_live_state(store, kernel):
    """The approved preview is 219→199. After another approved change makes the live
    price 209, the old approval no longer describes the write and must be refused."""
    from merchant_agent.changes import ChangeNotApplicable

    backend = EngineMerchant(store, kernel)
    stale = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    newer = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=209.00)]
    )
    backend.approve(stale.change_id, "user:acme-operator")
    backend.approve(newer.change_id, "user:acme-operator")

    await backend.apply_change(session(), newer.change_id)
    with pytest.raises(ChangeNotApplicable, match="changed since this change was staged"):
        await backend.apply_change(session(), stale.change_id)

    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN").price == 209.00
    assert stale.change_id in {
        pending.change_id for pending in await backend.get_pending_changes(session())
    }


async def test_concurrent_changes_for_one_target_use_the_reviewed_live_value(store, kernel):
    """Target locking makes the fresh-state check meaningful when separately approved
    changes race: exactly one reviewed 219-based write may win."""
    from merchant_agent.changes import ChangeNotApplicable

    backend = EngineMerchant(store, kernel)
    changes = [
        await backend.stage_price_update(
            session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=price)]
        )
        for price in (199.00, 209.00)
    ]
    for change in changes:
        backend.approve(change.change_id, "user:acme-operator")

    results = await asyncio.gather(
        *(backend.apply_change(session(), change.change_id) for change in changes),
        return_exceptions=True,
    )

    assert sum(getattr(result, "status", None) is ChangeStatus.APPLIED for result in results) == 1
    assert sum(isinstance(result, ChangeNotApplicable) for result in results) == 1


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

    store.commerce.products.create(
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


def _variant_price_text(store, sku):
    """The raw `product_variants.price` TEXT the engine actually holds -- not the float
    the binding parses it into, which is exactly what a float-arithmetic bug hides."""
    row = (
        store.readonly_sql()
        .execute("SELECT price FROM product_variants WHERE sku = ?", (sku,))
        .fetchone()
    )
    return row["price"]


async def test_an_applied_price_update_persists_a_two_place_decimal_string(store, kernel):
    backend = EngineMerchant(store, kernel)
    change = await backend.stage_price_update(
        session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.5)]
    )
    backend.approve(change.change_id, "user:acme-operator")
    await backend.apply_change(session(), change.change_id)

    assert _variant_price_text(store, "TENT-RIDGE-TAN") == "199.50"


async def test_an_applied_promotion_prices_in_decimal_not_float(store, kernel):
    """219.00 less 5% is 208.05. Computed in binary float it is 208.04999999999998, and
    that string would land in `product_variants.price`, which every later cart line,
    subtotal and order total reads back."""
    from merchant_agent.types import PromotionDraft

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_promotion(
        session(),
        PromotionDraft(
            name="Spring tents",
            listing_ids=["TENT-RIDGE-GRN", "TENT-RIDGE-TAN"],
            discount_pct=5.0,
            starts="2026-03-01",
            ends="2026-03-14",
        ),
    )
    # The staged figure is already the exact string the write will persist.
    assert {item.after for item in change.items} == {"208.05"}
    assert {item.before for item in change.items} == {"219.00"}

    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)
    assert applied.status is ChangeStatus.APPLIED

    for sku in ("TENT-RIDGE-GRN", "TENT-RIDGE-TAN"):
        assert _variant_price_text(store, sku) == "208.05"
        assert store.commerce.products.get_variant_by_sku(sku).price == 208.05

    # The promotion itself is recorded as a `promotion` custom object, and the evidence
    # for this ungoverned write is an activity-log id, never a receipt.
    record = store.commerce.custom_objects.get_object_by_handle("promotion", change.change_id)
    assert record is not None
    assert json.loads(record.values_json)["payload"]["name"] == "Spring tents"
    assert "activity log" in applied.guardrail_notes[-1]
    assert "receipt" not in applied.guardrail_notes[-1].lower()


async def test_a_promotion_price_flows_into_a_cart_line_as_a_currency_amount(store, kernel):
    """The reason the exact string matters: the promoted price is what every later cart
    line and subtotal is computed from, including the figures the host hands the
    browser as engine-vouched."""
    from merchant_agent.types import PromotionDraft
    from shopping_agent.types import ShoppingSessionContext

    from engine_backend.storefront import EngineStorefront

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_promotion(
        session(),
        PromotionDraft(
            name="Spring tents",
            listing_ids=["TENT-RIDGE-GRN"],
            discount_pct=5.0,
            starts="2026-03-01",
            ends="2026-03-14",
        ),
    )
    backend.approve(change.change_id, "user:acme-operator")
    await backend.apply_change(session(), change.change_id)

    customer = store.commerce.customers.get_by_email("rowan@example.invalid")
    store.bind("shop-1", customer.id, "customer")
    storefront = EngineStorefront(store)
    shopper = ShoppingSessionContext(session_id="shop-1", user_id=customer.id)
    await storefront.add_to_cart(shopper, "TENT-RIDGE-GRN", 2)
    totals = await storefront.cart_exact_totals(shopper)

    assert totals["line_totals_exact"]["TENT-RIDGE-GRN"] == "416.10"
    assert totals["subtotal_exact"] == "416.10"


async def test_an_applied_campaign_writes_a_campaign_custom_object(store, kernel):
    from merchant_agent.types import CampaignDraft

    backend = EngineMerchant(store, kernel)
    change = await backend.stage_campaign(
        session(),
        CampaignDraft(name="Spring push", objective="awareness", budget=2500.0),
    )
    backend.approve(change.change_id, "user:acme-operator")
    applied = await backend.apply_change(session(), change.change_id)

    assert applied.status is ChangeStatus.APPLIED
    campaigns = await backend.get_campaign_performance(session())
    assert [c.name for c in campaigns] == ["Spring push"]
    assert campaigns[0].budget == 2500.0
    # A campaign is not a governed command: activity-log evidence, no receipt.
    assert "activity log" in applied.guardrail_notes[-1]
    assert "receipt" not in applied.guardrail_notes[-1].lower()


async def test_campaign_update_preserves_fields_the_draft_omits(store, kernel):
    from merchant_agent.types import CampaignDraft

    backend = EngineMerchant(store, kernel)
    created = await backend.stage_campaign(
        session(),
        CampaignDraft(
            name="Spring push",
            objective="awareness",
            budget=2500.0,
            starts="2026-03-01",
            ends="2026-03-14",
        ),
    )
    backend.approve(created.change_id, "user:acme-operator")
    await backend.apply_change(session(), created.change_id)
    campaign = (await backend.get_campaign_performance(session()))[0]

    update = await backend.stage_campaign(
        session(),
        CampaignDraft(
            campaign_id=campaign.campaign_id,
            name=campaign.name,
            budget=3000.0,
        ),
    )
    backend.approve(update.change_id, "user:acme-operator")
    await backend.apply_change(session(), update.change_id)

    updated = (await backend.get_campaign_performance(session(), campaign.campaign_id))[0]
    assert updated.budget == 3000.0
    assert updated.objective == "awareness"
    assert updated.starts == "2026-03-01"
    assert updated.ends == "2026-03-14"


async def test_a_promotion_over_the_guardrail_cap_is_refused(store, kernel):
    from merchant_agent.changes import GuardrailViolation
    from merchant_agent.types import PromotionDraft

    backend = EngineMerchant(store, kernel)
    with pytest.raises(GuardrailViolation):
        await backend.stage_promotion(
            session(),
            PromotionDraft(
                name="Everything must go",
                listing_ids=["TENT-RIDGE-GRN"],
                discount_pct=80.0,
                starts="2026-03-01",
                ends="2026-03-14",
            ),
        )
    # Untouched: still exactly what the engine's own seeding write put there.
    assert store.commerce.products.get_variant_by_sku("TENT-RIDGE-GRN").price == 219.00


async def test_two_successive_applies_both_reach_the_engine_handle(store, kernel):
    """The regression this exists to catch: a direct-SQL write is not reliably visible to
    the `Commerce` handle this process already holds, so the *second* applied price change
    silently never reached the storefront -- listings, cart lines and `subtotal_exact` kept
    the stale price while the row on disk was correct. The host holds one `Commerce` per
    process and `catalog.list_variants` resolves through `get_variant_by_sku`, so this is
    the path every shopper read takes.

    The transient read-only connection in `_disk_price` is not scene-setting: any other
    connection opening and closing on the file is enough to trigger the incoherence, and a
    second reader, a backup, or a worker thread's collected read-only connection all do it.
    This end-to-end test passes even without the pin, because `stage_price_update`'s
    catalog read holds a connection open and accidentally masks the hazard. The isolating
    guard is :func:`tests.test_store.test_a_direct_sql_write_is_visible_to_the_engine_handle`,
    which fails without the pin.

    Each price is asserted three ways: on disk, on the engine handle, and through the
    storefront the shopper actually sees.
    """
    from shopping_agent.types import ShoppingSessionContext

    from engine_backend import money
    from engine_backend.storefront import EngineStorefront

    def _disk_price(sku):
        connection = sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
        try:
            return connection.execute(
                "SELECT price FROM product_variants WHERE sku = ?", (sku,)
            ).fetchone()[0]
        finally:
            connection.close()

    backend = EngineMerchant(store, kernel)
    storefront = EngineStorefront(store)
    customer = store.commerce.customers.get_by_email("rowan@example.invalid")
    store.bind("shop-1", customer.id, "customer")
    shopper = ShoppingSessionContext(session_id="shop-1", user_id=customer.id)

    for new_price in (199.00, 189.00, 179.00):
        change = await backend.stage_price_update(
            session(), [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=new_price)]
        )
        backend.approve(change.change_id, "user:acme-operator")
        await backend.apply_change(session(), change.change_id)

        expected = money.exact(new_price)
        assert _disk_price("TENT-RIDGE-TAN") == expected, "the row on disk is wrong"
        variant = store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN")
        assert variant.price_exact == expected, "the engine handle is serving a stale price"
        details = await storefront.get_product_details(shopper, "TENT-RIDGE-TAN")
        assert details is not None
        assert details.price == money.to_float(expected), "the storefront is serving a stale price"
