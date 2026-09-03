import sqlite3

from fastapi.testclient import TestClient

from host.app import create_app


def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "store.db")))


def _order_skus(tmp_path, order_number):
    """What the engine actually recorded on one committed order, read straight from the
    store file rather than through the host."""
    connection = sqlite3.connect(str(tmp_path / "store.db"))
    try:
        rows = connection.execute(
            "SELECT oi.sku FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.order_number = ? ORDER BY oi.sku",
            (order_number,),
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def test_health(tmp_path):
    assert client(tmp_path).get("/healthz").json()["status"] == "ok"


def test_a_session_binds_identity_server_side(tmp_path):
    c = client(tmp_path)
    body = c.post("/shopping/session").json()
    assert body["session_id"]
    assert "customer_id" not in body


def test_checkout_completes_the_cart_through_the_engine(tmp_path):
    c = client(tmp_path)
    session_id = c.post("/shopping/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}
    c.post(
        "/shopping/cart/add",
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
        headers=headers,
    )
    result = c.post("/shopping/checkout", headers=headers).json()
    assert result["order_number"]
    assert result["receipt"]["status"] == "succeeded"


def test_approving_a_change_requires_a_known_id(tmp_path):
    c = client(tmp_path)
    session_id = c.post("/merchant/session").json()["session_id"]
    response = c.post("/merchant/changes/chg-nope/approve", headers={"X-Session-Id": session_id})
    assert response.status_code == 404


def test_a_session_response_carries_no_identifying_field(tmp_path):
    c = client(tmp_path)
    shopping_body = c.post("/shopping/session").json()
    merchant_body = c.post("/merchant/session").json()
    identifying = {
        "customer_id",
        "user_id",
        "subject_id",
        "operator",
        "merchant_id",
        "email",
    }
    assert set(shopping_body) - {"session_id"} == set()
    assert set(merchant_body) - {"session_id"} == set()
    assert not identifying & set(shopping_body)
    assert not identifying & set(merchant_body)


def test_routes_reject_a_missing_or_unknown_session_id(tmp_path):
    c = client(tmp_path)
    assert c.post("/shopping/cart/add", json={"product_id": "x", "quantity": 1}).status_code == 401
    assert c.post("/shopping/checkout").status_code == 401
    assert (
        c.post(
            "/shopping/cart/add",
            json={"product_id": "x", "quantity": 1},
            headers={"X-Session-Id": "not-a-real-session"},
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/merchant/changes/chg-nope/approve",
            headers={"X-Session-Id": "not-a-real-session"},
        ).status_code
        == 401
    )


def test_chat_routes_reject_a_missing_or_unknown_session_id_before_any_model_call(tmp_path):
    # No ANTHROPIC_API_KEY is set in this test process; if the identity gate did not
    # sit in front of the model call, this would error out reaching the runtime
    # instead of cleanly 401ing, and that would be the finding.
    c = client(tmp_path)
    assert c.post("/shopping/chat", json={"message": "hi"}).status_code == 401
    assert c.post("/merchant/chat", json={"message": "hi"}).status_code == 401
    assert (
        c.post(
            "/shopping/chat",
            json={"message": "hi"},
            headers={"X-Session-Id": "not-a-real-session"},
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/merchant/chat",
            json={"message": "hi"},
            headers={"X-Session-Id": "not-a-real-session"},
        ).status_code
        == 401
    )


def test_a_session_cannot_be_used_for_the_other_role(tmp_path):
    """The binding's `kind` is what separates the two roles. A shopping session id on a
    merchant route (and the reverse) is rejected at the binding, before any backend,
    agent, or model call -- a 401, not a 404 or an empty result."""
    c = client(tmp_path)
    shopping = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    merchant = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}

    # A shopping session on the merchant routes.
    assert c.post("/merchant/chat", json={"message": "hi"}, headers=shopping).status_code == 401
    assert c.post("/merchant/changes/chg-nope/approve", headers=shopping).status_code == 401

    # A merchant session on the shopping routes.
    assert c.post("/shopping/chat", json={"message": "hi"}, headers=merchant).status_code == 401
    assert (
        c.post(
            "/shopping/cart/add",
            json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
            headers=merchant,
        ).status_code
        == 401
    )
    assert c.post("/shopping/checkout", headers=merchant).status_code == 401


def test_checkout_uses_the_session_s_own_cart_not_the_customer_s_last(tmp_path):
    """Every shopping session in this demo binds to the same seeded customer, so
    `carts.for_customer(...)[-1]` would hand one session another's cart. Two sessions
    each add a different item; each checkout must contain only its own.
    """
    c = client(tmp_path)
    first = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    second = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}

    c.post(
        "/shopping/cart/add",
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
        headers=first,
    )
    c.post(
        "/shopping/cart/add",
        json={"product_id": "LAMP-BEACON-BLK", "quantity": 1},
        headers=second,
    )

    # The second session was opened last, so a `for_customer(...)[-1]` checkout would
    # give the first session the headlamp cart.
    first_order = c.post("/shopping/checkout", headers=first).json()
    second_order = c.post("/shopping/checkout", headers=second).json()
    assert first_order["order_number"] and second_order["order_number"]
    assert first_order["order_number"] != second_order["order_number"]

    assert _order_skus(tmp_path, first_order["order_number"]) == ["TENT-RIDGE-GRN"]
    assert _order_skus(tmp_path, second_order["order_number"]) == ["LAMP-BEACON-BLK"]


def test_checkout_without_a_cart_is_refused(tmp_path):
    c = client(tmp_path)
    headers = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    assert c.post("/shopping/checkout", headers=headers).status_code == 409


def test_approving_a_change_that_is_no_longer_staged_is_refused(tmp_path):
    """Approval is only meaningful for a staged change. An already-applied one has
    nothing left to approve, and accepting it would put a live change id into the
    backend's `approved_ids` set."""
    import asyncio

    from merchant_agent.types import ChangeItem, ChangeKind, ChangeStatus

    from engine_backend import staging
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path))
    headers = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}

    store = EngineStore(db_path)
    change = staging.new_change(
        ChangeKind.PRICE_UPDATE,
        "Update price for TENT-RIDGE-TAN",
        [ChangeItem(target="TENT-RIDGE-TAN", field="price", before="219.00", after="199.00")],
        "user:acme-operator",
    )
    asyncio.run(staging.save(store, change))
    approve = f"/merchant/changes/{change.change_id}/approve"
    assert c.post(approve, headers=headers).status_code == 200

    asyncio.run(staging.save(store, change.model_copy(update={"status": ChangeStatus.APPLIED})))
    response = c.post(approve, headers=headers)
    assert response.status_code == 409
    assert "applied" in response.json()["detail"]
