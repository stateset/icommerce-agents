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


async def apply_change(
    ctx: ApplyContext, change: StagedChange, payload: Any = None
) -> tuple[StagedChange, list[staging.Evidence]]:
    """The engine write for one already-approved, already-guardrail-checked staged
    change, and the ``APPLIED`` copy of it, alongside the structured evidence the write
    actually produced. ``payload`` is the promotion or campaign draft the change was
    staged from (``engine_backend.staging.load_change_payload``); every other kind
    ignores it. The caller (``EngineMerchant.apply_change``) is responsible for loading
    the change, checking its status and approval, and persisting both of these."""
    results = await _apply_write(ctx, change, payload)
    notes = [note for note, _evidence in results]
    evidence = [item for _note, item in results]
    updated = change.model_copy(
        update={
            "status": ChangeStatus.APPLIED,
            "applied_at": staging.datetime.now(staging.UTC),
            "applied_by": ctx.operator,
            "guardrail_notes": [*change.guardrail_notes, *notes],
        }
    )
    return updated, evidence


async def _apply_write(
    ctx: ApplyContext, change: StagedChange, payload: Any
) -> list[tuple[str, staging.Evidence]]:
    """The platform write for one staged change. Each result pairs a human-readable
    note (still appended to ``guardrail_notes``, for a person reading the change
    history) with the structured evidence it is evidence of: an activity-log id for an
    ungoverned direct binding write, or a sealed kernel receipt id for a governed
    command. Raises, and leaves the change staged, on a failed write."""
    if change.kind is ChangeKind.PRICE_UPDATE:
        return await _apply_price_update(ctx, change)
    if change.kind is ChangeKind.INVENTORY_ACTION:
        return await _apply_inventory_action(ctx, change)
    if change.kind is ChangeKind.LISTING_UPDATE:
        return await _apply_listing_update(ctx, change)
    if change.kind is ChangeKind.PROMOTION:
        return await _apply_promotion(ctx, change, payload)
    if change.kind is ChangeKind.CAMPAIGN:
        return await _apply_campaign(ctx, change, payload)
    raise ChangeNotApplicable(f"unknown change kind {change.kind!r}")


def _update_variant_price_binding(commerce: Commerce, sku: str, price: float) -> None:
    """Update variant price using the binding mutator when available.

    Falls back to a direct-SQL path upstream has used historically only when the
    mutator is not present on the installed binding.
    """
    variant = commerce.products.get_variant_by_sku(sku)
    if variant is None:
        raise ChangeNotApplicable(f"no variant with sku {sku!r}")
    # Build a full variant input preserving existing fields that the update requires.
    # The Python binding's CreateProductVariantInput is exported in 1.28.5; updateVariant
    # requires sku and price and accepts optional fields.
    try:
        from stateset_embedded import CreateProductVariantInput  # local import for type
    except Exception as _e:  # pragma: no cover - defensive, binding is pinned in CI
        CreateProductVariantInput = None  # type: ignore[assignment]
    input_kwargs: dict[str, object] = {
        "sku": variant.sku,
        "price": price,
    }
    # Preserve optional fields when present on the variant object.
    if getattr(variant, "name", None) is not None:
        input_kwargs["name"] = variant.name
    if getattr(variant, "compare_at_price", None) is not None:
        input_kwargs["compare_at_price"] = variant.compare_at_price
    if getattr(variant, "is_default", None) is not None:
        input_kwargs["is_default"] = variant.is_default
    # Prefer camelCase mutator name exactly as exposed in the binding; snake_case fallback
    # is attempted in case the symbol is surfaced that way.
    if hasattr(commerce.products, "updateVariant"):
        update_payload = (
            CreateProductVariantInput(**input_kwargs) if CreateProductVariantInput else input_kwargs
        )
        commerce.products.updateVariant(variant.product_id, update_payload)  # type: ignore[arg-type]
        return
    if hasattr(commerce.products, "update_variant"):
        update_payload = (
            CreateProductVariantInput(**input_kwargs) if CreateProductVariantInput else input_kwargs
        )
        commerce.products.update_variant(variant.product_id, update_payload)  # type: ignore[arg-type]
        return
    # No mutator available on this binding: the caller will provide the legacy fallback.
    raise AttributeError("products.updateVariant/update_variant not available on binding")


def _update_product_binding(commerce: Commerce, product_id: str, **fields: object) -> None:
    """Update product fields via binding mutator when available (description/status)."""
    # Prefer update(id, { ... }) when present.
    if hasattr(commerce.products, "update"):
        commerce.products.update(product_id, fields)
        return
    raise AttributeError("products.update not available on binding")


async def _log_apply(
    ctx: ApplyContext, change: StagedChange, summary: str
) -> tuple[str, staging.Evidence]:
    """Record the apply as an activity-log entry, the evidence for an ungoverned write.
    ``activity_logs.record`` requires a real UUID for ``subject_id`` -- the engine has
    no notion of a ``chg-...`` staged-change id -- so the change is referenced by its
    own id in ``metadata`` instead, under the ``staged_change`` subject type.

    Routed through ``store.write`` under the same ``staged_change:{change_id}`` key
    ``staging.save`` uses for this change, so the log entry a write produced can never
    run concurrently with the write itself or with another log entry for the same
    change. ``apply_change`` (``merchant.py``) does not hold that lock while this apply
    runs -- it only takes it afterwards, in ``staging.save`` -- so acquiring it here
    does not nest under an outer holder of the same key."""

    def body(c: Any) -> Any:
        return c.activity_logs.record(
            subject_type=staging.STAGED_TYPE,
            subject_id=str(uuid4()),
            action="apply",
            summary=summary,
            actor_kind="user",
            actor=ctx.operator,
            metadata=json.dumps({"change_id": change.change_id}),
        )

    entry = await ctx.store.write(f"staged_change:{change.change_id}", body)
    note = f"applied via direct binding write; activity log {entry.id}"
    return note, staging.Evidence(kind="activity_log", id=entry.id, note=note)


async def _record_custom_object(
    ctx: ApplyContext, change: StagedChange, type_handle: str, handle: str, payload: Any
) -> None:
    """Routed through ``custom_objects.write_payload`` under the same
    ``staged_change:{change_id}`` key ``staging.save`` uses for this change (not
    necessarily ``handle`` itself -- a campaign's object handle is the campaign id,
    not the change id), so this write and the activity-log entry for the same apply
    (see ``_log_apply``) serialize against each other and against ``staging.save``."""
    await custom_objects.write_payload(
        ctx.store,
        type_handle,
        type_handle.replace("_", " ").title(),
        payload,
        lock_key=f"staged_change:{change.change_id}",
        object_handle=handle,
    )


async def _apply_price_update(
    ctx: ApplyContext, change: StagedChange
) -> list[tuple[str, staging.Evidence]]:
    results: list[tuple[str, staging.Evidence]] = []
    for item in change.items:
        if item.field != "price":
            continue
        price = money.exact(item.after)
        # Use binding mutator; fail loudly if unavailable.
        def body(c: Commerce, _sku: str = item.target, _p: float = float(price)) -> None:
            _update_variant_price_binding(c, _sku, _p)

        await ctx.store.write(f"price:{item.target}", body)
        results.append(await _log_apply(ctx, change, f"set price of {item.target} to {price}"))
    return results


async def _apply_inventory_action(
    ctx: ApplyContext, change: StagedChange
) -> list[tuple[str, staging.Evidence]]:
    results: list[tuple[str, staging.Evidence]] = []
    for item in change.items:
        if item.field == "stock":
            results.append(await _apply_restock(ctx, change, item))
        elif item.field == "status":
            results.append(await _apply_status_change(ctx, change, item))
        else:
            raise ChangeNotApplicable(f"unsupported inventory field {item.field!r}")
    return results


async def _apply_restock(
    ctx: ApplyContext, change: StagedChange, item: ChangeItem
) -> tuple[str, staging.Evidence]:
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
        await _log_apply(ctx, change, f"created inventory item {sku} via kernel command")
        note = (
            "governed via kernel command inventory.item.create; "
            f"sealed receipt {receipt.receipt_id}"
        )
        return note, staging.Evidence(kind="kernel_receipt", id=receipt.receipt_id, note=note)

    def body(c: Commerce) -> None:
        c.inventory.adjust(sku, added, reason="restock")

    await ctx.store.write(f"stock:{sku}", body)
    return await _log_apply(ctx, change, f"restocked {sku} by {added}")


async def _apply_status_change(
    ctx: ApplyContext, change: StagedChange, item: ChangeItem
) -> tuple[str, staging.Evidence]:
    resolved = await resolve_product_and_merch(ctx.store, item.target)
    if resolved is None:
        raise ChangeNotApplicable(f"no listing with id {item.target!r}")
    product, _merch = resolved
    # Prefer dedicated status mutators when present; fall back to a generic update; and
    # only then to the legacy direct-SQL write.
    desired = str(item.after)
    def body(c: Commerce) -> None:
        if desired == "active" and hasattr(c.products, "activate"):
            c.products.activate(product.id)
            return
        if desired == "archived" and hasattr(c.products, "archive"):
            c.products.archive(product.id)
            return
        # Generic products.update(id, { status })
        _update_product_binding(c, product.id, status=desired)

    await ctx.store.write(f"status:{product.id}", body)
    return await _log_apply(ctx, change, f"set status of {item.target} to {item.after}")


async def _apply_listing_update(
    ctx: ApplyContext, change: StagedChange
) -> list[tuple[str, staging.Evidence]]:
    resolved = await resolve_product_and_merch(ctx.store, change.items[0].target)
    if resolved is None:
        raise ChangeNotApplicable(f"no listing with id {change.items[0].target!r}")
    product, merch = resolved

    merch_field_names = {"brand", "category", "image_url", "long_description", "unit_cost"}
    updates: dict[str, Any] = {}
    attributes = dict(merch.attributes)
    for item in change.items:
        if item.field == "description":
            def body(c: Commerce, _desc: str = str(item.after)) -> None:
                _update_product_binding(c, product.id, description=_desc)

            await ctx.store.write(f"product:{product.id}", body)
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
    return [await _log_apply(ctx, change, f"updated listing content for {change.items[0].target}")]


async def _apply_promotion(
    ctx: ApplyContext, change: StagedChange, payload: Any
) -> list[tuple[str, staging.Evidence]]:
    for item in change.items:
        if item.field != "price":
            continue
        price = money.exact(item.after)
        def body(c: Commerce, _sku: str = item.target, _p: float = float(price)) -> None:
            _update_variant_price_binding(c, _sku, _p)

        await ctx.store.write(f"promo-price:{item.target}", body)
    await _record_custom_object(ctx, change, PROMOTION_TYPE, change.change_id, payload)
    return [await _log_apply(ctx, change, f"applied promotion {change.change_id}")]


async def _apply_campaign(
    ctx: ApplyContext, change: StagedChange, payload: Any
) -> list[tuple[str, staging.Evidence]]:
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
    await _record_custom_object(
        ctx, change, CAMPAIGN_TYPE, campaign_id, campaign.model_dump(mode="json")
    )
    return [await _log_apply(ctx, change, f"wrote campaign {campaign_id}")]
