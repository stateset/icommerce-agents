"""Read-only postcondition inspection for ambiguous merchant applies."""

from __future__ import annotations

from typing import Any, Literal

from merchant_agent.types import Campaign, ChangeKind, StagedChange
from pydantic import BaseModel

from engine_backend import custom_objects, money
from engine_backend.apply import CAMPAIGN_TYPE
from engine_backend.catalog import resolve_product_and_merch, resolve_variant_row, stock_reader
from engine_backend.store import EngineStore


class ReconciliationItem(BaseModel):
    target: str
    field: str
    before: Any = None
    intended_after: Any = None
    observed: Any = None
    state: Literal["matches_before", "matches_after", "diverged", "indeterminate"]


class ReconciliationAssessment(BaseModel):
    change_id: str
    outcome: Literal["not_applied", "applied", "partial_or_diverged", "indeterminate"]
    items: list[ReconciliationItem]


def _classified(
    target: str, field: str, before: Any, after: Any, observed: Any
) -> ReconciliationItem:
    state: Literal["matches_before", "matches_after", "diverged", "indeterminate"]
    if observed == after:
        state = "matches_after"
    elif observed == before:
        state = "matches_before"
    else:
        state = "diverged"
    return ReconciliationItem(
        target=target,
        field=field,
        before=before,
        intended_after=after,
        observed=observed,
        state=state,
    )


async def assess(
    store: EngineStore, change: StagedChange, payload: Any = None
) -> ReconciliationAssessment:
    """Compare current live state with both sides of the reviewed proposal."""
    items: list[ReconciliationItem] = []
    for item in change.items:
        if item.field == "price" and change.kind in (
            ChangeKind.PRICE_UPDATE,
            ChangeKind.PROMOTION,
        ):
            row = await resolve_variant_row(store, item.target)
            observed: Any = None if row is None else money.exact(row.variant.price_exact)
            items.append(
                _classified(
                    item.target,
                    item.field,
                    money.exact(item.before),
                    money.exact(item.after),
                    observed,
                )
            )
            continue

        if change.kind is ChangeKind.INVENTORY_ACTION and item.field == "stock":
            stock = await store.call(stock_reader(item.target))
            observed = None if stock is None else float(stock.total_available)
            classified = _classified(
                item.target,
                item.field,
                float(item.before),
                float(item.after),
                observed,
            )
            # An additive adjustment can land on the intended number coincidentally if
            # another writer moved stock. Report the observation, but never automate a
            # final judgment from it.
            classified.state = "indeterminate"
            items.append(classified)
            continue

        if change.kind is ChangeKind.INVENTORY_ACTION and item.field == "status":
            resolved = await resolve_product_and_merch(store, item.target)
            observed = None if resolved is None else resolved[0].status
            items.append(_classified(item.target, item.field, item.before, item.after, observed))
            continue

        if change.kind is ChangeKind.LISTING_UPDATE:
            resolved = await resolve_product_and_merch(store, item.target)
            if resolved is None:
                observed = None
            else:
                product, merch = resolved
                if item.field == "description":
                    observed = product.description
                elif item.field in {
                    "brand",
                    "category",
                    "image_url",
                    "long_description",
                    "unit_cost",
                }:
                    observed = getattr(merch, item.field)
                elif item.field in ("attributes", "specs"):
                    observed = dict(getattr(merch, item.field))
                elif item.field == "labels":
                    observed = list(merch.labels)
                else:
                    observed = merch.attributes.get(item.field)
            items.append(_classified(item.target, item.field, item.before, item.after, observed))
            continue

        if change.kind is ChangeKind.CAMPAIGN:
            campaign_id = payload.get("campaign_id") if payload else None
            current_payload = (
                await custom_objects.read_payload(store, CAMPAIGN_TYPE, object_handle=campaign_id)
                if campaign_id
                else None
            )
            current = Campaign.model_validate(current_payload) if current_payload else None
            observed = getattr(current, item.field, None) if current else None
            items.append(_classified(item.target, item.field, item.before, item.after, observed))
            continue

        items.append(
            ReconciliationItem(
                target=item.target,
                field=item.field,
                before=item.before,
                intended_after=item.after,
                state="indeterminate",
            )
        )

    states = {item.state for item in items}
    outcome: Literal["not_applied", "applied", "partial_or_diverged", "indeterminate"]
    if not items or "indeterminate" in states:
        outcome = "indeterminate"
    elif states == {"matches_after"}:
        outcome = "applied"
    elif states == {"matches_before"}:
        outcome = "not_applied"
    else:
        outcome = "partial_or_diverged"
    return ReconciliationAssessment(change_id=change.change_id, outcome=outcome, items=items)


__all__ = ["ReconciliationAssessment", "ReconciliationItem", "assess"]
