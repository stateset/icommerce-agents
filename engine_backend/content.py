"""Policies and disclosures as store-owned custom objects.

The engine has no document domain, so both live as custom object types this repo owns,
the same pattern ``catalog.py`` uses for merchandising. All text here is written by us
as seed data; the model never authors a row (see ``StorefrontBackend.get_disclosure``).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from shopping_agent.types import Disclosure, DisclosureRow, Policy
from stateset_embedded import Commerce

from engine_backend.custom_objects import ensure_payload_type, list_payloads, read_payload
from engine_backend.store import EngineStore

POLICY_TYPE = "policy_document"
POLICY_DISPLAY = "Policy document"
DISCLOSURE_TYPE = "disclosure"
DISCLOSURE_DISPLAY = "Disclosure"


class _PolicyRecord(BaseModel):
    policy_id: str
    title: str
    body: str
    category: str | None = None


class _DisclosureRecord(BaseModel):
    title: str
    product_id: str
    rows: list[dict[str, str | None]] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


_POLICIES: list[_PolicyRecord] = [
    _PolicyRecord(
        policy_id="pol-returns",
        title="Returns policy",
        category="returns",
        body=(
            "ACME Supply accepts returns of unused gear within 30 days of delivery in its "
            "original packaging. Refunds go back to the original payment method within five "
            "business days of the return reaching our warehouse. Worn or washed items, and "
            "gas canisters or fuel, are not eligible for return."
        ),
    ),
    _PolicyRecord(
        policy_id="pol-shipping",
        title="Shipping policy",
        category="shipping",
        body=(
            "ACME Supply ships from a single warehouse in the continental United States. "
            "Standard ground shipping arrives in five to seven business days, express in "
            "two to three, and overnight orders placed before 2pm local time ship the same "
            "day. Shipping is free on orders over $75."
        ),
    ),
    _PolicyRecord(
        policy_id="pol-warranty",
        title="Warranty policy",
        category="warranty",
        body=(
            "ACME Outdoors and ACME Gear products carry a two-year limited warranty against "
            "manufacturing defects in materials and workmanship, covering seam failures, "
            "zipper and buckle hardware, and pole or frame breakage under normal use. Damage "
            "from misuse, wear, or unauthorized repair is not covered."
        ),
    ),
    _PolicyRecord(
        policy_id="pol-privacy",
        title="Privacy policy",
        category="privacy",
        body=(
            "ACME Supply collects the contact and order information needed to fulfill a "
            "purchase and does not sell customer data to third parties. Order history and "
            "account details are retained for as long as the account is active and can be "
            "deleted on request."
        ),
    ),
]

_VIETNAM = "Assembled in Vietnam"
_TAIWAN = "Assembled in Taiwan"
_CHINA = "Assembled in China"

_DISCLOSURE_TEXT: dict[str, dict[str, str]] = {
    "TENT-RIDGE-GRN": {"material": "Ripstop nylon, aluminium poles", "origin": _VIETNAM},
    "TENT-RIDGE-TAN": {"material": "Ripstop nylon, aluminium poles", "origin": _VIETNAM},
    "BAG-SUMMIT-REG": {"material": "Synthetic fill, polyester shell", "origin": _VIETNAM},
    "BAG-SUMMIT-LNG": {"material": "Synthetic fill, polyester shell", "origin": _VIETNAM},
    "STOVE-TRAIL-1": {"material": "Anodized aluminium, brass valve", "origin": _TAIWAN},
    "PACK-SWITCH-SLT": {"material": "Recycled ripstop nylon", "origin": _VIETNAM},
    "PACK-SWITCH-MOS": {"material": "Recycled ripstop nylon", "origin": _VIETNAM},
    "FILTER-CLEAR-1": {"material": "Hollow-fiber membrane, ABS housing", "origin": _TAIWAN},
    "LAMP-BEACON-BLK": {"material": "Polycarbonate housing, lithium-ion cell", "origin": _CHINA},
    "LAMP-BEACON-ORG": {"material": "Polycarbonate housing, lithium-ion cell", "origin": _CHINA},
}


def _disclosure_records() -> list[_DisclosureRecord]:
    records = []
    for sku, facts in _DISCLOSURE_TEXT.items():
        records.append(
            _DisclosureRecord(
                title=f"Product facts — {sku}",
                product_id=sku,
                rows=[
                    {"label": "Materials", "value": facts["material"], "note": None},
                    {"label": "Country of origin", "value": facts["origin"], "note": None},
                    {
                        "label": "Warranty",
                        "value": "2-year limited warranty",
                        "note": "See the warranty policy for coverage details.",
                    },
                ],
                sources=["ACME Supply warranty policy"],
            )
        )
    return records


def ensure_content_types(commerce: Commerce) -> None:
    """Create the custom object types this module owns. Idempotent."""
    ensure_payload_type(commerce, POLICY_TYPE, POLICY_DISPLAY)
    ensure_payload_type(commerce, DISCLOSURE_TYPE, DISCLOSURE_DISPLAY)


def seed_content(commerce: Commerce) -> None:
    """Idempotent: returns immediately when policy documents are already seeded."""
    ensure_content_types(commerce)
    if commerce.custom_objects.list_objects(type_handle=POLICY_TYPE, limit=1):
        return

    for policy in _POLICIES:
        commerce.custom_objects.create_object(
            type_handle=POLICY_TYPE,
            values_json=json.dumps({"payload": policy.model_dump()}),
            owner_type="store",
            owner_id=policy.policy_id,
        )

    for disclosure in _disclosure_records():
        commerce.custom_objects.create_object(
            type_handle=DISCLOSURE_TYPE,
            values_json=json.dumps({"payload": disclosure.model_dump()}),
            owner_type="product",
            owner_id=disclosure.product_id,
        )


async def find_policies(store: EngineStore, query: str) -> list[Policy]:
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []

    payloads = await store.call(lambda c: list_payloads(c, POLICY_TYPE))
    scored: list[tuple[int, _PolicyRecord]] = []
    for payload in payloads:
        policy = _PolicyRecord.model_validate(payload)
        haystack = f"{policy.title} {policy.body}".lower()
        hits = sum(haystack.count(term) for term in terms)
        if hits > 0:
            scored.append((hits, policy))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        Policy(policy_id=p.policy_id, title=p.title, category=p.category, content=p.body)
        for _, p in scored
    ]


async def find_disclosure(store: EngineStore, product_id: str) -> Disclosure | None:
    payload = await read_payload(store, DISCLOSURE_TYPE, owner_type="product", owner_id=product_id)
    if payload is None:
        return None

    data = _DisclosureRecord.model_validate(payload)
    return Disclosure(
        title=data.title,
        product_id=data.product_id,
        rows=[DisclosureRow(**row) for row in data.rows],
        sources=list(data.sources),
    )
