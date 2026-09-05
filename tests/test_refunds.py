from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient

from engine_backend.store import EngineStore
from host.app import create_app
from host.auth import AuthConfig

AUTH_SECRET = "refund-test-secret-long-enough-for-hs256"


def _merchant(client: TestClient) -> dict[str, str]:
    session_id = client.post("/merchant/session").json()["session_id"]
    return {"X-Session-Id": session_id}


def _payment_id(db_path: str) -> str:
    return EngineStore(db_path).commerce.payments.list()[0].id


def test_refund_is_previewed_digest_bound_and_governed(tmp_path):
    db_path = str(tmp_path / "store.db")
    client = TestClient(create_app(db_path))
    headers = _merchant(client)
    payment_id = _payment_id(db_path)

    preview = client.post(
        "/merchant/refunds/preview",
        headers=headers,
        json={"payment_id": payment_id, "amount": "10.00"},
    )
    assert preview.status_code == 200
    proposal = preview.json()
    assert proposal["refund_amount"] == "10.00"
    assert proposal["captured_amount"] == "219.00"
    assert proposal["proposal_digest"].startswith("sha256:")

    tampered = client.post(
        "/merchant/refunds",
        headers=headers,
        json={
            "payment_id": payment_id,
            "amount": "11.00",
            "proposal_digest": proposal["proposal_digest"],
            "idempotency_key": "refund-tampered-1",
        },
    )
    assert tampered.status_code == 409

    applied = client.post(
        "/merchant/refunds",
        headers=headers,
        json={
            "payment_id": payment_id,
            "amount": "10.00",
            "proposal_digest": proposal["proposal_digest"],
            "idempotency_key": "refund-approved-1",
        },
    )
    assert applied.status_code == 200
    assert applied.json()["receipt"]["sealed"] is True
    assert applied.json()["receipt"]["status"] == "succeeded"


def test_engine_refuses_over_refund_through_operator_route(tmp_path):
    db_path = str(tmp_path / "store.db")
    client = TestClient(create_app(db_path))
    headers = _merchant(client)
    payment_id = _payment_id(db_path)
    preview = client.post(
        "/merchant/refunds/preview",
        headers=headers,
        json={"payment_id": payment_id, "amount": "10000.00"},
    ).json()
    response = client.post(
        "/merchant/refunds",
        headers=headers,
        json={
            "payment_id": payment_id,
            "amount": "10000.00",
            "proposal_digest": preview["proposal_digest"],
            "idempotency_key": "refund-too-large-1",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "commerce.refund.exceeds_captured"
    assert response.json()["detail"]["sealed"] is True


def test_refund_routes_require_a_merchant_session_and_validate_money(tmp_path):
    db_path = str(tmp_path / "store.db")
    client = TestClient(create_app(db_path))
    payment_id = _payment_id(db_path)
    assert (
        client.post(
            "/merchant/refunds/preview",
            json={"payment_id": payment_id, "amount": "10.00"},
        ).status_code
        == 401
    )
    headers = _merchant(client)
    assert (
        client.post(
            "/merchant/refunds/preview",
            headers=headers,
            json={"payment_id": payment_id, "amount": "10.001"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/merchant/refunds/preview",
            headers=headers,
            json={"payment_id": "missing", "amount": "10.00"},
        ).status_code
        == 404
    )


def test_refund_apply_requires_dedicated_permission_in_jwt_mode(tmp_path):
    auth = AuthConfig(
        mode="jwt",
        issuer="https://identity.example.test",
        audience="icommerce-host",
        hs256_secret=AUTH_SECRET,
    )
    client = TestClient(create_app(str(tmp_path / "store.db"), auth_config=auth))
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": auth.issuer,
            "aud": auth.audience,
            "sub": "operator:merchant-only",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "roles": ["merchant"],
            "store_id": "store:acme",
        },
        AUTH_SECRET,
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    session = client.post("/merchant/session", headers=headers)
    headers["X-Session-Id"] = session.json()["session_id"]

    response = client.post(
        "/merchant/refunds",
        headers=headers,
        json={
            "payment_id": "not-reached",
            "amount": "10.00",
            "proposal_digest": "sha256:" + "0" * 64,
            "idempotency_key": "refund-authz-check",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "payment refund access required"
