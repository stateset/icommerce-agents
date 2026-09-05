from fastapi.testclient import TestClient

from host.app import create_app


def test_metrics_are_disabled_until_a_monitoring_token_is_configured(engine_db):
    client = TestClient(create_app(engine_db("store.db")))
    assert client.get("/metrics").status_code == 404


def test_metrics_require_their_own_token_and_use_route_templates(engine_db, monkeypatch):
    token = "monitoring-secret-that-is-at-least-32-bytes"
    monkeypatch.setenv("ICOMMERCE_METRICS_TOKEN", token)
    client = TestClient(create_app(engine_db("store.db")))
    assert client.get("/metrics").status_code == 401

    session_id = client.post("/shopping/session").json()["session_id"]
    response = client.get(
        "/shopping/cart", headers={"X-Session-Id": session_id, "X-Request-Id": "metrics-test"}
    )
    assert response.status_code == 200

    metrics = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    assert (
        'icommerce_http_requests_total{method="GET",route="/shopping/cart",status="200"} 1'
        in metrics.text
    )
    assert (
        'icommerce_http_request_duration_seconds_count{method="GET",route="/shopping/cart"} 1'
        in metrics.text
    )
    assert (
        "icommerce_http_request_duration_seconds_bucket"
        '{method="GET",route="/shopping/cart",le="+Inf"} 1' in metrics.text
    )
    assert session_id not in metrics.text


def test_metrics_reject_a_weak_monitoring_token(engine_db, monkeypatch):
    monkeypatch.setenv("ICOMMERCE_METRICS_TOKEN", "too-short")
    try:
        create_app(engine_db("store.db"))
    except ValueError as error:
        assert "32 bytes" in str(error)
    else:
        raise AssertionError("weak metrics token was accepted")
