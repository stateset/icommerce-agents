"""The keyless tour: a narrated walk through the repo's own point, with no model in the
loop and no API key required.

Every step calls a real route against a real, seeded engine store and prints what the
engine actually returned -- a sealed checkout receipt, a refused unapproved apply, and
the two evidence shapes an applied merchant change can carry. Where no HTTP route
exists for a step (staging and applying a merchant change go through the model's tools
in a live deployment), this calls the same backend the host wires up
(``engine_backend.merchant.EngineMerchant``) directly, against the same on-disk store
the HTTP routes above already wrote to -- never a mock, never a fabricated result.

Exit 0 means every expected outcome happened, including the refusal: an unapproved
apply that *succeeded* is a failure this script reports, not a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import InventoryActionItem, MerchantSessionContext, PriceUpdateItem
from stateset_embedded import CreateProductVariantInput

from engine_backend.kernel import KernelClient
from engine_backend.merchant import EngineMerchant
from engine_backend.staging import load_evidence
from engine_backend.store import EngineStore
from host.app import create_app

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OPERATOR = "user:acme-operator"
CART_SKU = "TENT-RIDGE-GRN"
PRICE_UPDATE_SKU = "TENT-RIDGE-TAN"
NEW_SKU = "WIDGET-TOUR-NEW"


class TourResult:
    """What the tour actually observed, for `run_tour`'s caller and for tests to
    assert on -- structured outcomes, not narration text."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.ok = True
        self.order_number: str | None = None
        self.refused_unapproved_apply: bool | None = None
        self.evidence_kinds: list[str] = []
        self.session_ids: dict[str, str] = {}

    def narrate(self, line: str) -> None:
        print(line)
        self.steps.append(line)

    def fail(self, line: str) -> None:
        self.narrate(f"FAILED: {line}")
        self.ok = False


def run_tour(db_path: str) -> TourResult:
    """Drive the whole tour against a fresh engine store at ``db_path``. Returns a
    :class:`TourResult` -- always, even on failure -- so a caller (or a test) can
    inspect exactly which expected outcome went missing."""
    result = TourResult()
    client = TestClient(create_app(db_path))

    # -- Shopping: cart, checkout, the sealed receipt ----------------------------

    result.narrate("Opening a shopping session (POST /shopping/session)...")
    session_response = client.post("/shopping/session")
    session_id = session_response.json().get("session_id") if session_response.is_success else None
    if not session_id:
        result.fail(f"could not open a shopping session: {session_response.text}")
        return result
    result.session_ids["shopping"] = session_id
    shopping_headers = {"X-Session-Id": session_id}

    result.narrate(f"Adding {CART_SKU} to the cart (POST /shopping/cart/add)...")
    cart = client.post(
        "/shopping/cart/add",
        json={"product_id": CART_SKU, "quantity": 1},
        headers=shopping_headers,
    ).json()
    if not cart.get("items"):
        result.fail(f"cart is empty after add: {cart}")
        return result

    cart_read = client.get("/shopping/cart", headers=shopping_headers).json()
    if not cart_read.get("items"):
        result.fail(f"GET /shopping/cart did not show the item just added: {cart_read}")
        return result

    result.narrate("Placing the order through POST /shopping/checkout...")
    checkout = client.post("/shopping/checkout", headers=shopping_headers).json()
    receipt = checkout.get("receipt") or {}
    order_number = checkout.get("order_number")
    if not order_number or receipt.get("status") != "succeeded":
        result.fail(f"checkout did not produce a sealed, succeeded receipt: {checkout}")
        return result
    result.order_number = order_number
    result.narrate(f"Order {order_number} placed. Sealed receipt: {receipt}")

    orders = client.get("/shopping/orders", headers=shopping_headers).json().get("orders", [])
    if not any(order.get("total_exact") for order in orders):
        result.fail(f"GET /shopping/orders did not carry an exact total: {orders}")
        return result
    result.narrate(f"GET /shopping/orders shows {len(orders)} order(s) for this session.")

    # -- Merchant: stage, refuse, approve, apply, evidence -----------------------

    result.narrate("Opening a merchant session (POST /merchant/session)...")
    merchant_session_response = client.post("/merchant/session")
    merchant_session_id = (
        merchant_session_response.json().get("session_id")
        if merchant_session_response.is_success
        else None
    )
    if not merchant_session_id:
        result.fail(f"could not open a merchant session: {merchant_session_response.text}")
        return result
    result.session_ids["merchant"] = merchant_session_id
    merchant_headers = {"X-Session-Id": merchant_session_id}

    # Staging and applying have no HTTP route in this host (only chat, which needs a
    # model, and approve, which this script also uses over HTTP below) -- so this talks
    # to the same `EngineMerchant` the host wires up, directly, against the very store
    # file the HTTP calls above already wrote to. Nothing here is mocked or faked.
    store = EngineStore(db_path)
    kernel = KernelClient(
        store, CONFIG_DIR / "kernel-policy.json", CONFIG_DIR / "kernel-principal.json"
    )
    merchant = EngineMerchant(store, kernel)
    backend_session = MerchantSessionContext(
        session_id=merchant_session_id, merchant_id=store.store_id, operator=OPERATOR
    )

    result.narrate(f"Staging a price update for {PRICE_UPDATE_SKU}...")
    price_change = asyncio.run(
        merchant.stage_price_update(
            backend_session, [PriceUpdateItem(listing_id=PRICE_UPDATE_SKU, new_price=199.00)]
        )
    )

    result.narrate(f"Attempting to apply {price_change.change_id} with NO approval...")
    try:
        asyncio.run(merchant.apply_change(backend_session, price_change.change_id))
    except ChangeNotApplicable as error:
        result.refused_unapproved_apply = True
        result.narrate(f"Refused, as expected: {error}")
    else:
        result.refused_unapproved_apply = False
        result.fail(f"the unapproved apply of {price_change.change_id} SUCCEEDED")
        return result

    result.narrate(
        f"Approving {price_change.change_id} "
        f"(POST /merchant/changes/{price_change.change_id}/approve)..."
    )
    approve_response = client.post(
        f"/merchant/changes/{price_change.change_id}/approve", headers=merchant_headers
    )
    if approve_response.status_code != 200:
        result.fail(f"approve did not succeed: {approve_response.text}")
        return result
    # The HTTP call above recorded the approval on the host's own `EngineMerchant`
    # instance; this script's local one needs the same mark before it can apply.
    merchant.approve(price_change.change_id, OPERATOR)

    result.narrate(f"Applying {price_change.change_id}...")
    applied_price = asyncio.run(merchant.apply_change(backend_session, price_change.change_id))
    price_evidence = asyncio.run(load_evidence(store, applied_price.change_id))
    if [item.kind for item in price_evidence] != ["activity_log"]:
        result.fail(f"expected activity_log evidence, got: {price_evidence}")
        return result
    result.evidence_kinds.append(price_evidence[0].kind)
    result.narrate(f"Applied. activity_log evidence: {price_evidence[0].model_dump()}")

    result.narrate(f"Creating a brand-new listing ({NEW_SKU}) with no inventory item yet...")
    store.commerce.products.create(
        name="Tour Widget",
        description="A widget with no inventory item yet, created for the tour.",
        variants=[CreateProductVariantInput(sku=NEW_SKU, price=25.00)],
    )

    result.narrate(f"Staging a restock of {NEW_SKU}...")
    restock_change = asyncio.run(
        merchant.stage_inventory_action(
            backend_session,
            [InventoryActionItem(listing_id=NEW_SKU, action="restock", quantity=20)],
        )
    )

    result.narrate(f"Approving {restock_change.change_id}...")
    approve_restock = client.post(
        f"/merchant/changes/{restock_change.change_id}/approve", headers=merchant_headers
    )
    if approve_restock.status_code != 200:
        result.fail(f"approve did not succeed: {approve_restock.text}")
        return result
    merchant.approve(restock_change.change_id, OPERATOR)

    result.narrate(f"Applying {restock_change.change_id}...")
    applied_restock = asyncio.run(merchant.apply_change(backend_session, restock_change.change_id))
    restock_evidence = asyncio.run(load_evidence(store, applied_restock.change_id))
    if [item.kind for item in restock_evidence] != ["kernel_receipt"]:
        result.fail(f"expected kernel_receipt evidence, got: {restock_evidence}")
        return result
    result.evidence_kinds.append(restock_evidence[0].kind)
    result.narrate(f"Applied. kernel_receipt evidence: {restock_evidence[0].model_dump()}")

    changes = client.get("/merchant/changes", headers=merchant_headers).json().get("changes", [])
    seen_ids = {item["change_id"] for item in changes}
    if not {price_change.change_id, restock_change.change_id} <= seen_ids:
        result.fail(f"GET /merchant/changes did not carry both applied changes: {changes}")
        return result
    result.narrate(f"GET /merchant/changes shows {len(changes)} change(s), both with evidence.")

    result.narrate("Tour complete: order placed, one refusal, two evidence kinds recorded.")
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="tour-demo.db", help="path to the store db file")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    print("=" * 72)
    result = run_tour(args.db)
    print("=" * 72)
    if result.ok:
        print("Tour succeeded.")
        return 0
    print("Tour did NOT complete as expected -- see FAILED lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
