from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from host.app import create_app
from host.auth import AuthConfig

ISSUER = "https://identity.example.test"
AUDIENCE = "icommerce-host"
SECRET = "a-test-only-secret-that-is-long-enough-for-hs256"


def _config(**updates):
    values = {
        "mode": "jwt",
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "hs256_secret": SECRET,
    }
    values.update(updates)
    return AuthConfig(**values)


def _token(*, subject="identity:rowan", **claims):
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_jwt_mode_fails_closed_when_verifier_configuration_is_incomplete(tmp_path):
    with pytest.raises(ValueError, match="ISSUER"):
        create_app(str(tmp_path / "store.db"), AuthConfig(mode="jwt"))
    with pytest.raises(ValueError, match="exactly one"):
        create_app(
            str(tmp_path / "other.db"),
            _config(jwks_url="https://identity.example.test/jwks.json"),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(
            str(tmp_path / "insecure.db"),
            _config(hs256_secret=None, jwks_url="http://identity.example.test/jwks.json"),
        )
    with pytest.raises(ValueError, match="32 bytes"):
        create_app(str(tmp_path / "weak.db"), _config(hs256_secret="too-short"))


def test_host_rejects_unreasonably_short_session_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("ICOMMERCE_SESSION_TTL_SECONDS", "59")
    with pytest.raises(ValueError, match="session TTL"):
        create_app(str(tmp_path / "store.db"))


def test_jwt_mode_rejects_missing_expired_and_wrong_audience_tokens(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    missing = client.post("/shopping/session", headers={"Origin": "http://localhost:3000"})
    assert missing.status_code == 401
    assert missing.headers["access-control-allow-origin"] == "http://localhost:3000"
    oversized = {"Authorization": "Bearer " + "x" * 16_385}
    assert client.post("/shopping/session", headers=oversized).status_code == 401

    now = datetime.now(UTC)
    expired = _token(exp=now - timedelta(seconds=1), roles=["customer"])
    assert client.post("/shopping/session", headers=_bearer(expired)).status_code == 401

    wrong_audience = _token(aud="some-other-service", roles=["customer"])
    assert client.post("/shopping/session", headers=_bearer(wrong_audience)).status_code == 401


def test_host_adds_correlation_and_security_headers_without_reflecting_bad_ids(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db")))
    response = client.get("/healthz", headers={"X-Request-Id": "release:check-7"})
    assert response.headers["x-request-id"] == "release:check-7"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"

    invalid = client.get("/healthz", headers={"X-Request-Id": "not valid value"})
    assert invalid.headers["x-request-id"] != "not valid value"
    assert len(invalid.headers["x-request-id"]) == 32


def test_verified_customer_can_only_open_and_use_a_shopping_session(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    token = _token(roles=["customer"], email="rowan@example.invalid")
    headers = _bearer(token)
    opened = client.post("/shopping/session", headers=headers)
    assert opened.status_code == 200
    headers["X-Session-Id"] = opened.json()["session_id"]
    assert client.get("/shopping/cart", headers=headers).status_code == 200
    assert client.post("/merchant/session", headers=_bearer(token)).status_code == 403


def test_authenticated_deployments_cannot_create_unpaid_orders(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    token = _token(roles=["customer"], email="rowan@example.invalid")
    headers = _bearer(token)
    session_id = client.post("/shopping/session", headers=headers).json()["session_id"]
    headers["X-Session-Id"] = session_id
    added = client.post(
        "/shopping/cart/add",
        headers=headers,
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
    )
    assert added.status_code == 200
    missing = client.post("/shopping/checkout", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == (
        "direct checkout is demo-only; use a configured payment rail"
    )

    checkout = client.post(
        "/shopping/checkout",
        headers=headers,
        json={
            "shipping_address": {
                "first_name": "Rowan",
                "last_name": "Quinn",
                "line1": "42 Cedar Street",
                "city": "Vancouver",
                "state": "BC",
                "postal_code": "V6B 1A1",
                "country": "CA",
            }
        },
    )
    assert checkout.status_code == 404


def test_production_profile_rejects_demo_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("ICOMMERCE_ENVIRONMENT", "production")
    monkeypatch.setenv("ICOMMERCE_ALLOWED_ORIGINS", "https://shop.example.com")
    monkeypatch.setenv("ICOMMERCE_METRICS_TOKEN", "m" * 32)
    with pytest.raises(ValueError, match="ICOMMERCE_AUTH_MODE must be jwt"):
        create_app(str(tmp_path / "production.db"))


def test_production_profile_accepts_asymmetric_auth_and_https_origins(monkeypatch, tmp_path):
    monkeypatch.setenv("ICOMMERCE_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ICOMMERCE_ALLOWED_ORIGINS",
        "https://shop.example.com,https://merchant.example.com",
    )
    monkeypatch.setenv("ICOMMERCE_METRICS_TOKEN", "m" * 32)
    app = create_app(
        str(tmp_path / "production.db"),
        auth_config=AuthConfig(
            mode="jwt",
            issuer="https://identity.example.com/",
            audience="icommerce-host",
            jwks_url="https://identity.example.com/.well-known/jwks.json",
        ),
    )
    assert app is not None


def test_verified_merchant_is_tenant_scoped_and_becomes_the_operator(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    wrong_store = _token(roles=["merchant"], store_id="store:other")
    assert client.post("/merchant/session", headers=_bearer(wrong_store)).status_code == 403

    token = _token(subject="identity:merchant-7", scope="merchant:write", store_id="store:acme")
    opened = client.post("/merchant/session", headers=_bearer(token))
    assert opened.status_code == 200
    headers = {**_bearer(token), "X-Session-Id": opened.json()["session_id"]}
    assert client.get("/merchant/changes", headers=headers).status_code == 200


def test_a_stolen_session_id_cannot_be_used_by_another_verified_subject(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    owner = _token(roles=["merchant"], store_id="store:acme")
    opened = client.post("/merchant/session", headers=_bearer(owner)).json()
    attacker = _token(
        subject="identity:attacker",
        roles=["merchant"],
        store_id="store:acme",
    )
    response = client.get(
        "/merchant/changes",
        headers={**_bearer(attacker), "X-Session-Id": opened["session_id"]},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "session subject mismatch"


def test_role_and_tenant_are_rechecked_after_session_creation(tmp_path):
    client = TestClient(create_app(str(tmp_path / "store.db"), _config()))
    owner = _token(roles=["merchant"], store_id="store:acme")
    session_id = client.post("/merchant/session", headers=_bearer(owner)).json()["session_id"]

    no_longer_merchant = _token()
    headers = {**_bearer(no_longer_merchant), "X-Session-Id": session_id}
    assert client.get("/merchant/changes", headers=headers).status_code == 403

    wrong_tenant = _token(roles=["merchant"], store_id="store:other")
    headers = {**_bearer(wrong_tenant), "X-Session-Id": session_id}
    assert client.get("/merchant/changes", headers=headers).status_code == 403
