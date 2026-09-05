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

Re-running against an existing db (the second thing a curious reader does) must
produce the same narrated arc and exit 0, not crash on state the first run left
behind: the restock step's brand-new listing gets a per-run-unique SKU and slug for
exactly that reason (mirrors the fix in ``scripts/denials.py`` for the same wart).

Two ways to point this at an engine: ``db_path`` alone (the default) builds and holds
the FastAPI app in-process via ``TestClient`` -- the convenient path for a one-shot
run. Passing ``base_url`` instead drives an *already-running* host over real HTTP,
which is what a caller wanting to point the tour at a live ``uvicorn`` process (as
``run_demo.py --tour`` will, in Task 3) needs.

Staging and applying a merchant change have no HTTP route (only chat, which needs a
model), so both modes still open a direct ``EngineStore``/``EngineMerchant`` against
``db_path`` for those two calls -- and, honestly, for the whole merchant section
built on them: the handle is opened once, before that section starts, and stays open
across both ``approve`` round-trips, ``products.create``, both ``apply_change``
calls, and the final ``GET /merchant/changes``, not just for the two calls that force
it. In ``base_url`` mode that is a second live ``EngineStore`` handle held open on the
same db file the running host already holds open, concurrently, across several HTTP
round-trips to that host -- the same two-handles-on-one-file shape
``engine_backend.store.EngineStore``'s own pin exists to make safe, not a narrower
window than that. Narrowing it to just the two calls that force it would mean
rebuilding the store/kernel/merchant trio (and re-resolving ``backend_session``) twice
mid-flow for no correctness gain, so this leaves the window as-is and says so
plainly rather than describing a smaller one.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import InventoryActionItem, MerchantSessionContext, PriceUpdateItem
from stateset_embedded import CreateProductVariantInput

from engine_backend.kernel import KernelClient
from engine_backend.merchant import EngineMerchant
from engine_backend.staging import load_evidence, load_record
from engine_backend.store import EngineStore
from host.app import create_app

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OPERATOR = "user:acme-operator"
CART_SKU = "TENT-RIDGE-GRN"
PRICE_UPDATE_SKU = "TENT-RIDGE-TAN"


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


def run_tour(
    db_path: str,
    *,
    base_url: str | None = None,
    session_ids: dict[str, str] | None = None,
) -> TourResult:
    """Drive the whole tour. ``db_path`` names the engine store either way -- the one
    ``TestClient`` builds in-process (``base_url`` omitted), or the one an
    already-running host at ``base_url`` holds open (``base_url`` given): the merchant
    stage/apply steps need it directly regardless, since neither has an HTTP route.

    ``session_ids`` lets a caller that already holds open shopping/merchant sessions
    (against the same store) hand them in as ``{"shopping": ..., "merchant": ...}``
    instead of this minting fresh ones. Returns a :class:`TourResult` -- always, even
    on failure -- so a caller (or a test) can inspect exactly which expected outcome
    went missing."""
    result = TourResult()
    session_ids = dict(session_ids or {})
    http_client: Any = (
        httpx.Client(base_url=base_url) if base_url else TestClient(create_app(db_path))
    )
    with http_client as http:
        return _drive_tour(http, db_path, session_ids, result)


def _drive_tour(
    http: Any, db_path: str, session_ids: dict[str, str], result: TourResult
) -> TourResult:
    """The body of the tour, run inside `run_tour`'s `with` block so `http` (an
    `httpx.Client` in `base_url` mode, or the `TestClient` `run_tour` built) is always
    closed -- on an early return as much as on the happy path."""
    # -- Shopping: cart, checkout, the sealed receipt ----------------------------

    session_id = session_ids.get("shopping")
    if session_id:
        result.narrate(f"Reusing shopping session {session_id}...")
    else:
        result.narrate("Opening a shopping session (POST /shopping/session)...")
        session_response = http.post("/shopping/session")
        session_id = (
            session_response.json().get("session_id") if session_response.is_success else None
        )
        if not session_id:
            result.fail(f"could not open a shopping session: {session_response.text}")
            return result
    result.session_ids["shopping"] = session_id
    shopping_headers = {"X-Session-Id": session_id}

    result.narrate(f"Adding {CART_SKU} to the cart (POST /shopping/cart/add)...")
    cart = http.post(
        "/shopping/cart/add",
        json={"product_id": CART_SKU, "quantity": 1},
        headers=shopping_headers,
    ).json()
    if not cart.get("items"):
        result.fail(f"cart is empty after add: {cart}")
        return result

    cart_read = http.get("/shopping/cart", headers=shopping_headers).json()
    if not cart_read.get("items"):
        result.fail(f"GET /shopping/cart did not show the item just added: {cart_read}")
        return result

    result.narrate("Placing the order through POST /shopping/checkout...")
    checkout = http.post("/shopping/checkout", headers=shopping_headers).json()
    receipt = checkout.get("receipt") or {}
    order_number = checkout.get("order_number")
    if not order_number or receipt.get("status") != "succeeded":
        result.fail(f"checkout did not produce a sealed, succeeded receipt: {checkout}")
        return result
    result.order_number = order_number
    result.narrate(f"Order {order_number} placed. Sealed receipt: {receipt}")

    orders = http.get("/shopping/orders", headers=shopping_headers).json().get("orders", [])
    if not any(order.get("total_exact") for order in orders):
        result.fail(f"GET /shopping/orders did not carry an exact total: {orders}")
        return result
    result.narrate(f"GET /shopping/orders shows {len(orders)} order(s) for this session.")

    # -- Merchant: stage, refuse, approve, apply, evidence -----------------------

    merchant_session_id = session_ids.get("merchant")
    if merchant_session_id:
        result.narrate(f"Reusing merchant session {merchant_session_id}...")
    else:
        result.narrate("Opening a merchant session (POST /merchant/session)...")
        merchant_session_response = http.post("/merchant/session")
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
    # model), so this talks to the same `EngineMerchant` the host wires up, directly,
    # against the very store file the HTTP calls above already wrote to -- nothing
    # here is mocked or faked. This handle stays open for the rest of the merchant
    # section below (both approves, `products.create`, both applies, the final
    # `GET /merchant/changes`), not just for the stage/apply calls that force it: in
    # `base_url` mode that means a second live `EngineStore` on the same db file the
    # running host already holds open, concurrently, across several HTTP round-trips
    # to it -- the two-handles-on-one-file shape `EngineStore`'s own pin exists to
    # make safe, held for longer than the two calls that strictly need it.
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
    approve_response = http.post(
        f"/merchant/changes/{price_change.change_id}/approve",
        headers=merchant_headers,
        json={
            "proposal_digest": asyncio.run(load_record(store, price_change.change_id))[
                "proposal_digest"
            ]
        },
    )
    if approve_response.status_code != 200:
        result.fail(f"approve did not succeed: {approve_response.text}")
        return result
    # Approval is durable shared-store state. The separate backend instance below sees
    # the host route's exact digest-bound ledger entry; no in-process mirroring exists.

    result.narrate(f"Applying {price_change.change_id}...")
    applied_price = asyncio.run(merchant.apply_change(backend_session, price_change.change_id))
    price_evidence = asyncio.run(load_evidence(store, applied_price.change_id))
    if [item.kind for item in price_evidence] != ["activity_log"]:
        result.fail(f"expected activity_log evidence, got: {price_evidence}")
        return result
    result.evidence_kinds.append(price_evidence[0].kind)
    result.narrate(f"Applied. activity_log evidence: {price_evidence[0].model_dump()}")

    # A per-run suffix on both the name and the SKU, not a fixed one: a rerun against
    # an existing `--db` file would otherwise hit "Duplicate product slug" on this
    # `create` call (the slug derives from the name) -- the exact wart just fixed in
    # `scripts/denials.py`'s idempotency key, here in the script whose whole job is
    # surviving a second run.
    run_id = secrets.token_hex(4)
    new_sku = f"WIDGET-TOUR-{run_id}"
    result.narrate(f"Creating a brand-new listing ({new_sku}) with no inventory item yet...")
    store.commerce.products.create(
        name=f"Tour Widget {run_id}",
        description="A widget with no inventory item yet, created for the tour.",
        variants=[CreateProductVariantInput(sku=new_sku, price=25.00)],
    )

    result.narrate(f"Staging a restock of {new_sku}...")
    restock_change = asyncio.run(
        merchant.stage_inventory_action(
            backend_session,
            [InventoryActionItem(listing_id=new_sku, action="restock", quantity=20)],
        )
    )

    result.narrate(f"Approving {restock_change.change_id}...")
    approve_restock = http.post(
        f"/merchant/changes/{restock_change.change_id}/approve",
        headers=merchant_headers,
        json={
            "proposal_digest": asyncio.run(load_record(store, restock_change.change_id))[
                "proposal_digest"
            ]
        },
    )
    if approve_restock.status_code != 200:
        result.fail(f"approve did not succeed: {approve_restock.text}")
        return result
    result.narrate(f"Applying {restock_change.change_id}...")
    applied_restock = asyncio.run(merchant.apply_change(backend_session, restock_change.change_id))
    restock_evidence = asyncio.run(load_evidence(store, applied_restock.change_id))
    if [item.kind for item in restock_evidence] != ["kernel_receipt"]:
        result.fail(f"expected kernel_receipt evidence, got: {restock_evidence}")
        return result
    result.evidence_kinds.append(restock_evidence[0].kind)
    result.narrate(f"Applied. kernel_receipt evidence: {restock_evidence[0].model_dump()}")

    changes = http.get("/merchant/changes", headers=merchant_headers).json().get("changes", [])
    seen_ids = {item["change_id"] for item in changes}
    if not {price_change.change_id, restock_change.change_id} <= seen_ids:
        result.fail(f"GET /merchant/changes did not carry both applied changes: {changes}")
        return result
    result.narrate(f"GET /merchant/changes shows {len(changes)} change(s), both with evidence.")

    result.narrate("Tour complete: order placed, one refusal, two evidence kinds recorded.")
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/tour-demo.db", help="path to the store db file")
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "drive an already-running host at this URL over HTTP, instead of building "
            "one in-process; --db must still name the db file that host is using"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    print("=" * 72)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    result = run_tour(args.db, base_url=args.base_url)
    print("=" * 72)
    if result.ok:
        print("Tour succeeded.")
        return 0
    print("Tour did NOT complete as expected -- see FAILED lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
