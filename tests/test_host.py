from fastapi.testclient import TestClient

from host.app import create_app


def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "store.db")))


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
