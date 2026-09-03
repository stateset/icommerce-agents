"""Staged merchant changes, persisted in the engine so they survive a host restart.

Each ``StagedChange`` is a custom object of type ``staged_change``, keyed by its
``change_id`` as the object handle, following the pattern in catalog.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from merchant_agent.types import ActorKind, ChangeItem, ChangeKind, ChangeStatus, StagedChange
from stateset_embedded import Commerce

from engine_backend.custom_objects import (
    ensure_payload_type,
    list_payloads,
    read_payload,
    write_payload,
)
from engine_backend.store import EngineStore

STAGED_TYPE = "staged_change"
STAGED_DISPLAY = "Staged change"


def ensure_types(commerce: Commerce) -> None:
    """Create the ``staged_change`` custom object type. Idempotent."""
    ensure_payload_type(commerce, STAGED_TYPE, STAGED_DISPLAY)


async def save(store: EngineStore, change: StagedChange) -> None:
    await write_payload(
        store,
        STAGED_TYPE,
        STAGED_DISPLAY,
        change.model_dump(mode="json"),
        lock_key=f"staged_change:{change.change_id}",
        object_handle=change.change_id,
    )


async def load(store: EngineStore, change_id: str) -> StagedChange | None:
    payload = await read_payload(store, STAGED_TYPE, object_handle=change_id)
    if payload is None:
        return None
    return StagedChange.model_validate(payload)


async def pending(store: EngineStore) -> list[StagedChange]:
    payloads = await store.call(lambda c: list_payloads(c, STAGED_TYPE))
    changes = [StagedChange.model_validate(p) for p in payloads]
    return [c for c in changes if c.status is ChangeStatus.STAGED]


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
