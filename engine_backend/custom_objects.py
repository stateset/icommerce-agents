"""The one shape this repo stores in the engine's custom objects: a type with a single
required JSON ``payload`` field, one object per record.

Four things the engine has no domain for are kept this way -- merchandising
(``catalog.py``), policies and disclosures (``content.py``), staged changes
(``staging.py``), and applied promotions and campaigns (``apply.py``). Each names its own
type handle and says how an object is keyed -- by its own handle, or by the owner it
hangs off -- and the create/update, ensure-type and payload-parsing mechanics live here
once rather than in each of them.
"""

from __future__ import annotations

import json
from typing import Any

from stateset_embedded import Commerce, CustomFieldDefinitionInput

from engine_backend.store import EngineStore


def ensure_payload_type(commerce: Commerce, handle: str, display_name: str) -> None:
    """Create the ``handle`` type with its one required JSON ``payload`` field. Idempotent."""
    if commerce.custom_objects.get_type_by_handle(handle) is None:
        commerce.custom_objects.create_type(
            handle=handle,
            display_name=display_name,
            fields=[CustomFieldDefinitionInput(key="payload", field_type="json", required=True)],
        )


def payload_of(record: Any) -> Any:
    """The ``payload`` field of one custom object record, decoded."""
    return json.loads(record.values_json)["payload"]


def find_object(
    commerce: Commerce,
    handle: str,
    *,
    object_handle: str | None = None,
    owner_type: str | None = None,
    owner_id: str | None = None,
) -> Any:
    """One object of type ``handle``: the one with ``object_handle`` as its own handle, or
    the first one owned by ``owner_type``/``owner_id``."""
    if object_handle is not None:
        return commerce.custom_objects.get_object_by_handle(handle, object_handle)
    objects = commerce.custom_objects.list_objects(
        type_handle=handle, owner_type=owner_type, owner_id=owner_id, limit=1
    )
    return objects[0] if objects else None


def list_payloads(commerce: Commerce, handle: str) -> list[Any]:
    """Every payload of type ``handle``, decoded, in the engine's own order."""
    records = commerce.custom_objects.list_objects(type_handle=handle)
    return [payload_of(record) for record in records]


def put_payload(
    commerce: Commerce,
    handle: str,
    display_name: str,
    payload: Any,
    *,
    object_handle: str | None = None,
    owner_type: str | None = None,
    owner_id: str | None = None,
) -> None:
    """Create or update the one object ``object_handle`` / the owner pair names, after
    ensuring its type exists."""
    ensure_payload_type(commerce, handle, display_name)
    values = json.dumps({"payload": payload})
    record = find_object(
        commerce, handle, object_handle=object_handle, owner_type=owner_type, owner_id=owner_id
    )
    if record is not None:
        commerce.custom_objects.update_object(id=record.id, values_json=values)
        return
    extra: dict[str, str] = {}
    if object_handle is not None:
        extra["handle"] = object_handle
    if owner_type is not None:
        extra["owner_type"] = owner_type
    if owner_id is not None:
        extra["owner_id"] = owner_id
    commerce.custom_objects.create_object(type_handle=handle, values_json=values, **extra)


async def read_payload(
    store: EngineStore,
    handle: str,
    *,
    object_handle: str | None = None,
    owner_type: str | None = None,
    owner_id: str | None = None,
) -> Any:
    """The payload of one object, or ``None`` when there is no such object."""
    record = await store.call(
        lambda c: find_object(
            c, handle, object_handle=object_handle, owner_type=owner_type, owner_id=owner_id
        )
    )
    return None if record is None else payload_of(record)


async def write_payload(
    store: EngineStore,
    handle: str,
    display_name: str,
    payload: Any,
    *,
    lock_key: str,
    object_handle: str | None = None,
    owner_type: str | None = None,
    owner_id: str | None = None,
) -> None:
    """:func:`put_payload` under ``lock_key``, the store's own per-key write lock."""
    await store.write(
        lock_key,
        lambda c: put_payload(
            c,
            handle,
            display_name,
            payload,
            object_handle=object_handle,
            owner_type=owner_type,
            owner_id=owner_id,
        ),
    )
