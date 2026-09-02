"""Staged merchant changes, persisted in the engine so they survive a host restart.

Each ``StagedChange`` is a custom object of type ``staged_change``, keyed by its
``change_id`` as the object handle, following the pattern in catalog.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from merchant_agent.types import ActorKind, ChangeItem, ChangeKind, ChangeStatus, StagedChange
from stateset_embedded import Commerce

from engine_backend.store import EngineStore

STAGED_TYPE = "staged_change"


def ensure_types(commerce: Commerce) -> None:
    """Create the ``staged_change`` custom object type. Idempotent."""
    from stateset_embedded import CustomFieldDefinitionInput

    if commerce.custom_objects.get_type_by_handle(STAGED_TYPE) is None:
        commerce.custom_objects.create_type(
            handle=STAGED_TYPE,
            display_name="Staged change",
            fields=[CustomFieldDefinitionInput(key="payload", field_type="json", required=True)],
        )


async def save(store: EngineStore, change: StagedChange) -> None:
    values = json.dumps({"payload": change.model_dump(mode="json")})

    def body(c: Commerce) -> None:
        ensure_types(c)
        record = c.custom_objects.get_object_by_handle(STAGED_TYPE, change.change_id)
        if record is None:
            c.custom_objects.create_object(
                type_handle=STAGED_TYPE, values_json=values, handle=change.change_id
            )
        else:
            c.custom_objects.update_object(id=record.id, values_json=values)

    await store.write(f"staged_change:{change.change_id}", body)


async def load(store: EngineStore, change_id: str) -> StagedChange | None:
    def body(c: Commerce):
        return c.custom_objects.get_object_by_handle(STAGED_TYPE, change_id)

    record = await store.call(body)
    if record is None:
        return None
    return StagedChange.model_validate(json.loads(record.values_json)["payload"])


async def pending(store: EngineStore) -> list[StagedChange]:
    def body(c: Commerce):
        return c.custom_objects.list_objects(type_handle=STAGED_TYPE)

    records = await store.call(body)
    changes = [StagedChange.model_validate(json.loads(r.values_json)["payload"]) for r in records]
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
