"""The five-kind apply dispatch: the one place ``EngineMerchant`` mutates live state.

**The repo's central finding lives here.** Only one write out of the five kinds is
governed: a restock of a SKU with no inventory item yet goes through the kernel
command ``inventory.item.create`` and its evidence is a sealed receipt. Every other
write here -- price, listing content, promotion, campaign, pause/activate, and even a
restock of a SKU the store already tracks -- is a direct binding write (or, for three
fields the binding exposes no mutator for, direct SQL through ``EngineStore.write_sql``)
plus an activity-log id. ``docs/enforcement.md`` has the full table; keep it in sync
with this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import Campaign, ChangeItem, ChangeKind, ChangeStatus, StagedChange
from stateset_embedded import Commerce

from engine_backend import custom_objects, money, staging
from engine_backend.catalog import (
    resolve_product_and_merch,
    resolve_variant_row,
    write_merchandising,
)
from engine_backend.kernel import KernelClient
from engine_backend.store import EngineStore

PROMOTION_TYPE = "promotion"
CAMPAIGN_TYPE = "campaign"


@dataclass
class ApplyContext:
    store: EngineStore
    kernel: KernelClient
    operator: str


async def apply_change(ctx: ApplyContext, change: StagedChange) -> StagedChange:
    """The engine write for one already-approved, already-guardrail-checked staged
    change, and the ``APPLIED`` copy of it. The caller (``EngineMerchant.apply_change``)
    is responsible for loading the change, checking its status and approval, and
    persisting the result this returns."""
    extra_notes = await _apply_write(ctx, change)
    return change.model_copy(
        update={
            "status": ChangeStatus.APPLIED,
            "applied_at": staging.datetime.now(staging.UTC),
            "applied_by": ctx.operator,
            "guardrail_notes": [*change.guardrail_notes, *extra_notes],
        }
    )


async def _apply_write(ctx: ApplyContext, change: StagedChange) -> list[str]:
    """The platform write for one staged change. Returns extra ``guardrail_notes``
    recording the evidence: an activity-log id for an ungoverned direct binding write,
    or a sealed kernel receipt id for a governed command. Raises, and leaves the change
    staged, on a failed write."""
    if change.kind is ChangeKind.PRICE_UPDATE:
        return await _apply_price_update(ctx, change)
    if change.kind is ChangeKind.INVENTORY_ACTION:
        return await _apply_inventory_action(ctx, change)
    if change.kind is ChangeKind.LISTING_UPDATE:
        return await _apply_listing_update(ctx, change)
    if change.kind is ChangeKind.PROMOTION:
        return await _apply_promotion(ctx, change)
    if change.kind is ChangeKind.CAMPAIGN:
        return await _apply_campaign(ctx, change)
    raise ChangeNotApplicable(f"unknown change kind {change.kind!r}")


def _log_apply(ctx: ApplyContext, change: StagedChange, summary: str) -> str:
    """Record the apply as an activity-log entry, the evidence for an ungoverned write.
    ``activity_logs.record`` requires a real UUID for ``subject_id`` -- the engine has
    no notion of a ``chg-...`` staged-change id -- so the change is referenced by its
    own id in ``metadata`` instead, under the ``staged_change`` subject type."""
    entry = ctx.store.commerce.activity_logs.record(
        subject_type=staging.STAGED_TYPE,
        subject_id=str(uuid4()),
        action="apply",
        summary=summary,
        actor_kind="user",
        actor=ctx.operator,
        metadata=json.dumps({"change_id": change.change_id}),
    )
    return f"applied via direct binding write; activity log {entry.id}"


def _record_custom_object(
    ctx: ApplyContext, type_handle: str, handle: str, payload_json: str
) -> None:
    """Synchronous and unlocked, unlike ``staging``'s writes: it runs inside an apply
    that already holds the change, on the store's own ``Commerce`` handle."""
    custom_objects.put_payload(
        ctx.store.commerce,
        type_handle,
        type_handle.replace("_", " ").title(),
        json.loads(payload_json),
        object_handle=handle,
    )


async def _apply_price_update(ctx: ApplyContext, change: StagedChange) -> list[str]:
    notes: list[str] = []
    for item in change.items:
        if item.field != "price":
            continue
        price = money.exact(item.after)
        await ctx.store.write_sql(
            "UPDATE product_variants SET price = ?, "
            "updated_at = datetime('now'), version = version + 1 WHERE sku = ?",
            (price, item.target),
        )
        notes.append(_log_apply(ctx, change, f"set price of {item.target} to {price}"))
    return notes


async def _apply_inventory_action(ctx: ApplyContext, change: StagedChange) -> list[str]:
    notes: list[str] = []
    for item in change.items:
        if item.field == "stock":
            notes.append(await _apply_restock(ctx, change, item))
        elif item.field == "status":
            notes.append(await _apply_status_change(ctx, change, item))
        else:
            raise ChangeNotApplicable(f"unsupported inventory field {item.field!r}")
    return notes


async def _apply_restock(ctx: ApplyContext, change: StagedChange, item: ChangeItem) -> str:
    sku = item.target
    added = float(item.after) - float(item.before)
    stock = await ctx.store.call(lambda c: c.inventory.get_stock(sku))
    if stock is None:
        # No inventory item exists yet: the engine only governs bringing a SKU into
        # its stock ledger, so this restock goes through the kernel.
        product_row = await resolve_variant_row(ctx.store, sku)
        name = product_row.product.name if product_row else sku
        receipt = await ctx.kernel.execute(
            "inventory.item.create",
            {
                "sku": sku,
                "name": name,
                "initial_quantity": str(added),
                "reorder_point": "5",
            },
            idempotency_key=f"{change.change_id}:{sku}",
        )
        if not receipt.ok or not receipt.sealed:
            # `sealed` is False for a receipt this process synthesized locally
            # (kernel.py) rather than one the engine actually sealed -- that is
            # never evidence of a governed write, whatever `ok` says.
            raise RuntimeError(
                f"inventory.item.create for {sku!r} failed: "
                f"{receipt.error_code} {receipt.error_message}"
            )
        _log_apply(ctx, change, f"created inventory item {sku} via kernel command")
        return (
            "governed via kernel command inventory.item.create; "
            f"sealed receipt {receipt.receipt_id}"
        )

    def body(c: Commerce) -> None:
        c.inventory.adjust(sku, added, reason="restock")

    await ctx.store.write(f"stock:{sku}", body)
    return _log_apply(ctx, change, f"restocked {sku} by {added}")


async def _apply_status_change(ctx: ApplyContext, change: StagedChange, item: ChangeItem) -> str:
    resolved = await resolve_product_and_merch(ctx.store, item.target)
    if resolved is None:
        raise ChangeNotApplicable(f"no listing with id {item.target!r}")
    product, _merch = resolved
    await ctx.store.write_sql(
        "UPDATE products SET status = ?, "
        "updated_at = datetime('now'), version = version + 1 WHERE id = ?",
        (str(item.after), product.id),
    )
    return _log_apply(ctx, change, f"set status of {item.target} to {item.after}")


async def _apply_listing_update(ctx: ApplyContext, change: StagedChange) -> list[str]:
    notes: list[str] = []
    resolved = await resolve_product_and_merch(ctx.store, change.items[0].target)
    if resolved is None:
        raise ChangeNotApplicable(f"no listing with id {change.items[0].target!r}")
    product, merch = resolved

    merch_field_names = {"brand", "category", "image_url", "long_description", "unit_cost"}
    updates: dict[str, Any] = {}
    attributes = dict(merch.attributes)
    for item in change.items:
        if item.field == "description":
            await ctx.store.write_sql(
                "UPDATE products SET description = ?, "
                "updated_at = datetime('now'), version = version + 1 WHERE id = ?",
                (str(item.after), product.id),
            )
        elif item.field in merch_field_names:
            updates[item.field] = item.after
        elif item.field == "attributes" and isinstance(item.after, dict):
            attributes.update(item.after)
        elif item.field == "specs" and isinstance(item.after, dict):
            updates["specs"] = {**merch.specs, **item.after}
        elif item.field == "labels" and isinstance(item.after, list):
            updates["labels"] = list(item.after)
        else:
            attributes[item.field] = item.after
    updates["attributes"] = attributes

    updated_merch = merch.model_copy(update=updates)
    await write_merchandising(ctx.store, product.id, updated_merch)
    notes.append(_log_apply(ctx, change, f"updated listing content for {change.items[0].target}"))
    return notes


async def _apply_promotion(ctx: ApplyContext, change: StagedChange) -> list[str]:
    notes: list[str] = []
    for item in change.items:
        if item.field != "price":
            continue
        await ctx.store.write_sql(
            "UPDATE product_variants SET price = ?, "
            "updated_at = datetime('now'), version = version + 1 WHERE sku = ?",
            (money.exact(item.after), item.target),
        )
    _record_custom_object(ctx, PROMOTION_TYPE, change.change_id, change.guardrail_notes[0])
    notes.append(_log_apply(ctx, change, f"applied promotion {change.change_id}"))
    return notes


async def _apply_campaign(ctx: ApplyContext, change: StagedChange) -> list[str]:
    payload = json.loads(change.guardrail_notes[0])
    campaign_id = payload.get("campaign_id") or f"camp-{uuid4().hex[:8]}"
    campaign = Campaign(
        campaign_id=campaign_id,
        name=payload["name"],
        status="active",
        objective=payload.get("objective"),
        budget=payload.get("budget") or 0.0,
        starts=payload.get("starts"),
        ends=payload.get("ends"),
    )
    _record_custom_object(ctx, CAMPAIGN_TYPE, campaign_id, campaign.model_dump_json())
    return [_log_apply(ctx, change, f"wrote campaign {campaign_id}")]
