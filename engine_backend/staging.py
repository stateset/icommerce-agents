"""Staged merchant changes: the ``stage_*`` half of ``MerchantBackend``, and the
persistence beneath it.

Each ``StagedChange`` is a custom object of type ``staged_change``, keyed by its
``change_id`` as the object handle, following the pattern in catalog.py. Staging a
change never writes live state -- that is ``engine_backend/apply.py``'s job, once a
staged change is approved -- so every function here ends at ``save``, not at a mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from commerce_common.fencing import truncate_display
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation, check_guardrails
from merchant_agent.config import MerchantAgentConfig
from merchant_agent.types import (
    ActorKind,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeStatus,
    InventoryActionItem,
    PriceUpdateItem,
    PromotionDraft,
    StagedChange,
)
from pydantic import BaseModel
from stateset_embedded import Commerce

from engine_backend import money
from engine_backend.catalog import catalog_rows, resolve_product_and_merch
from engine_backend.custom_objects import (
    ensure_payload_type,
    list_payloads,
    read_payload,
    write_payload,
)
from engine_backend.store import EngineStore

STAGED_TYPE = "staged_change"
STAGED_DISPLAY = "Staged change"

# A private sentinel, not ``None``: ``save`` must tell "the caller left this alone" (keep
# whatever is already stored) apart from "the caller explicitly wants this cleared", and
# ``None`` is a legitimate explicit value for ``payload`` (every non-promotion/campaign
# change has no draft at all).
_UNSET: Any = object()


class Evidence(BaseModel):
    """What actually backed one write inside an applied change: a sealed kernel
    receipt id for a governed command, or an activity-log id for an ungoverned direct
    binding write. Set once, at apply time, from what happened -- never inferred from
    ``guardrail_notes`` prose. ``note`` mirrors the human-readable line appended to
    ``guardrail_notes`` for the same write, kept here so a reader of the structured
    field never has to go looking for it, but nothing parses ``note`` back apart."""

    kind: Literal["kernel_receipt", "activity_log"]
    id: str
    note: str


def ensure_types(commerce: Commerce) -> None:
    """Create the ``staged_change`` custom object type. Idempotent."""
    ensure_payload_type(commerce, STAGED_TYPE, STAGED_DISPLAY)


async def save(
    store: EngineStore,
    change: StagedChange,
    *,
    evidence: list[Evidence] | None = None,
    payload: Any = _UNSET,
) -> None:
    """Persist ``change``, plus two things upstream's ``StagedChange`` has no room for:
    the structured ``evidence`` an apply produced, and (for a promotion or campaign) the
    ``payload`` draft this change was staged from. Both sit as extra keys alongside the
    vendor model's own fields in the record this writes -- ``StagedChange.model_validate``
    ignores unknown keys, so ``load`` below sees exactly the vendor fields back.

    Neither extra field is required on every call: a caller that does not pass
    ``evidence`` gets ``[]`` (there is none before a change is applied), but a caller
    that does not pass ``payload`` gets back whatever is already stored, not ``None`` --
    ``apply_change``'s own save call reports evidence without repeating the promotion or
    campaign draft it never touched, and a bare default here would silently erase it."""
    existing = await read_payload(store, STAGED_TYPE, object_handle=change.change_id)
    stored_payload = None if existing is None else existing.get("payload")
    record = change.model_dump(mode="json")
    record["evidence"] = [item.model_dump() for item in evidence or []]
    record["payload"] = stored_payload if payload is _UNSET else payload
    await write_payload(
        store,
        STAGED_TYPE,
        STAGED_DISPLAY,
        record,
        lock_key=f"staged_change:{change.change_id}",
        object_handle=change.change_id,
    )


async def load(store: EngineStore, change_id: str) -> StagedChange | None:
    record = await read_payload(store, STAGED_TYPE, object_handle=change_id)
    if record is None:
        return None
    return StagedChange.model_validate(record)


async def load_evidence(store: EngineStore, change_id: str) -> list[Evidence]:
    """The structured evidence an apply recorded for ``change_id``, or ``[]`` for a
    change that has not been applied (or does not exist)."""
    record = await read_payload(store, STAGED_TYPE, object_handle=change_id)
    if record is None:
        return []
    return [Evidence.model_validate(item) for item in record.get("evidence") or []]


async def load_change_payload(store: EngineStore, change_id: str) -> Any:
    """The draft a promotion or campaign change was staged from, or ``None`` for every
    other kind (and for an unknown change id)."""
    record = await read_payload(store, STAGED_TYPE, object_handle=change_id)
    return None if record is None else record.get("payload")


async def pending(store: EngineStore) -> list[StagedChange]:
    payloads = await store.call(lambda c: list_payloads(c, STAGED_TYPE))
    changes = [StagedChange.model_validate(p) for p in payloads]
    return [c for c in changes if c.status is ChangeStatus.STAGED]


async def discard(
    store: EngineStore, change_id: str, operator: str, actor_kind: ActorKind
) -> StagedChange:
    change = await load(store, change_id)
    if change is None:
        raise ChangeNotApplicable(f"no change with id {change_id!r} to discard")
    if change.status is not ChangeStatus.STAGED:
        raise ChangeNotApplicable(
            f"change {change_id} is {change.status.value}, not staged — nothing to discard"
        )
    discarded = change.model_copy(
        update={
            "status": ChangeStatus.DISCARDED,
            "discarded_at": datetime.now(UTC),
            "discarded_by": operator,
            "discarded_by_kind": actor_kind,
        }
    )
    await save(store, discarded)
    return discarded


def new_change(
    kind: ChangeKind,
    summary: str,
    items: list[ChangeItem],
    operator: str,
    currency: str | None = None,
    guardrail_notes: list[str] | None = None,
) -> StagedChange:
    return StagedChange(
        change_id=f"chg-{uuid4().hex[:12]}",
        kind=kind,
        summary=summary,
        items=items,
        created_at=datetime.now(UTC),
        created_by=operator,
        created_by_kind=ActorKind.OPERATOR,
        currency=currency,
        guardrail_notes=guardrail_notes or [],
    )


async def stage_listing_update(
    store: EngineStore,
    config: MerchantAgentConfig,
    operator: str,
    listing_id: str,
    fields: dict[str, Any],
) -> StagedChange:
    """Content and attribute edits. ``description`` writes the ``Product`` record's own
    description column (the engine's binding has no ``products.update``, so this goes
    through the direct SQL path in ``engine_backend.apply``'s listing-update dispatch,
    guarded the same way ``catalog.py`` reads variants the binding does not expose);
    every other known field maps onto its ``Merchandising`` counterpart in the listing's
    own custom object. ``price`` and ``stock`` are refused here by upstream's
    guardrails -- they are staged as a price update or an inventory action instead."""
    resolved = await resolve_product_and_merch(store, listing_id)
    if resolved is None:
        raise ChangeNotApplicable(f"no listing with id {listing_id!r}")
    product, merch = resolved

    field_map = {
        "brand": "brand",
        "category": "category",
        "image_url": "image_url",
        "long_description": "long_description",
        "unit_cost": "unit_cost",
    }
    change_items: list[ChangeItem] = []
    for field, value in fields.items():
        if field == "description":
            before = product.description
        elif field in field_map:
            before = getattr(merch, field_map[field])
        elif field in ("attributes", "specs") and isinstance(getattr(merch, field, None), dict):
            before = dict(getattr(merch, field))
        elif field in ("labels",):
            before = list(merch.labels)
        else:
            before = merch.attributes.get(field)
        change_items.append(ChangeItem(target=listing_id, field=field, before=before, after=value))

    violations = check_guardrails(ChangeKind.LISTING_UPDATE, change_items, config)
    if violations:
        raise GuardrailViolation(violations)

    summary = truncate_display(f"Update listing content for {listing_id}", 200)
    change = new_change(ChangeKind.LISTING_UPDATE, summary, change_items, operator)
    await save(store, change)
    return change


async def stage_price_update(
    store: EngineStore,
    config: MerchantAgentConfig,
    operator: str,
    items: list[PriceUpdateItem],
) -> StagedChange:
    rows = await catalog_rows(store)
    by_sku = {row.variant.sku: row for row in rows}
    change_items: list[ChangeItem] = []
    for item in items:
        row = by_sku.get(item.listing_id)
        if row is None:
            raise ChangeNotApplicable(f"no listing or variant with id {item.listing_id!r}")
        # Both sides stay exact decimal strings: `after` is what `apply.py`'s
        # `_apply_price_update` writes into `product_variants.price`, and upstream's
        # guardrails read either side with `float(...)`, which parses a string just as
        # well.
        change_items.append(
            ChangeItem(
                target=item.listing_id,
                field="price",
                before=money.exact(row.variant.price_exact),
                after=money.exact(item.new_price),
            )
        )

    violations = check_guardrails(ChangeKind.PRICE_UPDATE, change_items, config)
    if violations:
        raise GuardrailViolation(violations)

    summary = truncate_display("Update price for " + ", ".join(i.listing_id for i in items), 200)
    change = new_change(ChangeKind.PRICE_UPDATE, summary, change_items, operator, currency="USD")
    await save(store, change)
    return change


async def stage_inventory_action(
    store: EngineStore,
    config: MerchantAgentConfig,
    operator: str,
    items: list[InventoryActionItem],
) -> StagedChange:
    """A restock's ``before``/``after`` are stock levels; a pause/reactivation's are the
    product's status. Only a restock of a SKU with no inventory item yet is governed
    (``inventory.item.create``) -- that determination is made again, against live
    state, at apply time; see ``engine_backend.apply.apply_change``."""
    change_items: list[ChangeItem] = []
    for item in items:
        if item.action == "restock":
            stock = await store.call(lambda c, sku=item.listing_id: c.inventory.get_stock(sku))
            before = float(stock.total_available) if stock is not None else 0.0
            quantity = item.quantity or 0
            change_items.append(
                ChangeItem(
                    target=item.listing_id,
                    field="stock",
                    before=before,
                    after=before + quantity,
                )
            )
        else:
            resolved = await resolve_product_and_merch(store, item.listing_id)
            if resolved is None:
                raise ChangeNotApplicable(f"no listing with id {item.listing_id!r}")
            product, _merch = resolved
            before_status = product.status
            after_status = "active" if item.action == "activate" else "paused"
            change_items.append(
                ChangeItem(
                    target=item.listing_id,
                    field="status",
                    before=before_status,
                    after=after_status,
                )
            )

    violations = check_guardrails(ChangeKind.INVENTORY_ACTION, change_items, config)
    if violations:
        raise GuardrailViolation(violations)

    summary = truncate_display(
        "Inventory action for " + ", ".join(i.listing_id for i in items), 200
    )
    change = new_change(ChangeKind.INVENTORY_ACTION, summary, change_items, operator)
    await save(store, change)
    return change


async def stage_promotion(
    store: EngineStore,
    config: MerchantAgentConfig,
    operator: str,
    promotion: PromotionDraft,
) -> StagedChange:
    rows = await catalog_rows(store)
    by_sku = {row.variant.sku: row for row in rows}
    change_items: list[ChangeItem] = []
    for listing_id in promotion.listing_ids:
        row = by_sku.get(listing_id)
        if row is None:
            raise ChangeNotApplicable(f"no listing or variant with id {listing_id!r}")
        # The discount is applied in `Decimal`, to the engine's own exact string, and
        # quantized back to two places. Computing it in `float` would stage (and then
        # persist) a figure like 208.04999999999998 as this variant's price.
        before = money.exact(row.variant.price_exact)
        after = money.discounted(before, promotion.discount_pct)
        change_items.append(
            ChangeItem(target=listing_id, field="price", before=before, after=after)
        )

    violations = check_guardrails(ChangeKind.PROMOTION, change_items, config)
    if violations:
        raise GuardrailViolation(violations)

    summary = truncate_display(f"Promotion {promotion.name!r}", 200)
    change = new_change(ChangeKind.PROMOTION, summary, change_items, operator, currency="USD")
    # The draft goes on the record's own `payload` field, not into `guardrail_notes` --
    # that field is guardrail prose shown to the model, not a JSON blob for apply.py to
    # read back positionally.
    await save(store, change, payload=promotion.model_dump(mode="json"))
    return change


async def stage_campaign(
    store: EngineStore,
    config: MerchantAgentConfig,
    operator: str,
    campaign: CampaignDraft,
    existing: Campaign | None,
) -> StagedChange:
    """``existing`` is the campaign ``campaign.campaign_id`` already names, looked up by
    the caller (``EngineMerchant.get_campaign_performance``) -- staging has no campaign
    lookup of its own."""
    change_items: list[ChangeItem] = []
    if campaign.budget is not None:
        change_items.append(
            ChangeItem(
                target=campaign.campaign_id or campaign.name,
                field="budget",
                before=existing.budget if existing else None,
                after=campaign.budget,
            )
        )
    else:
        change_items.append(
            ChangeItem(
                target=campaign.campaign_id or campaign.name,
                field="name",
                before=existing.name if existing else None,
                after=campaign.name,
            )
        )

    violations = check_guardrails(ChangeKind.CAMPAIGN, change_items, config)
    if violations:
        raise GuardrailViolation(violations)

    summary = truncate_display(f"Campaign {campaign.name!r}", 200)
    change = new_change(
        ChangeKind.CAMPAIGN,
        summary,
        change_items,
        operator,
        currency="USD" if campaign.budget is not None else None,
    )
    await save(store, change, payload=campaign.model_dump(mode="json"))
    return change
