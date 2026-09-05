import base64
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from engine_backend.stablecoins import (
    FacilitatorResult,
    FacilitatorUncertain,
    HttpStablecoinRefundProvider,
    HttpX402Facilitator,
    RefundResult,
    RefundUncertain,
    StablecoinConfig,
)
from host.app import create_app
from host.auth import AuthConfig

PAYER = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x2222222222222222222222222222222222222222"
USDC = "0x3333333333333333333333333333333333333333"
ADDRESS = {
    "first_name": "Avery",
    "last_name": "Shopper",
    "line1": "100 Market Street",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94105",
    "country": "US",
}
AUTH_SECRET = "stablecoin-test-secret-long-enough-for-hs256"


class FakeFacilitator:
    def __init__(self, *, settle_uncertain: bool = False, payer: str = PAYER) -> None:
        self.settle_uncertain = settle_uncertain
        self.payer = payer
        self.verify_calls = 0
        self.settle_calls = 0

    async def verify(self, payment_payload, requirements):
        self.verify_calls += 1
        assert payment_payload["x402Version"] == 2
        assert requirements["amount"] == "219000000"
        return FacilitatorResult(success=True, payer=self.payer, network="eip155:8453")

    async def settle(self, payment_payload, requirements):
        self.settle_calls += 1
        if self.settle_uncertain:
            raise FacilitatorUncertain("settlement outcome is unknown")
        return FacilitatorResult(
            success=True,
            payer=self.payer,
            network="eip155:8453",
            transaction="0x" + "a" * 64,
        )


class FakeRefundProvider:
    def __init__(self, *, uncertain: bool = False, rejected: bool = False) -> None:
        self.uncertain = uncertain
        self.rejected = rejected
        self.calls: list[tuple[dict, str]] = []

    async def refund(self, request, idempotency_key):
        self.calls.append((request, idempotency_key))
        if self.uncertain:
            raise RefundUncertain("refund outcome is unknown")
        return RefundResult(
            success=not self.rejected,
            transaction=None if self.rejected else "0x" + "d" * 64,
            reason="treasury rejected" if self.rejected else None,
            network="eip155:8453",
        )


def _config(*, refunds: bool = False) -> StablecoinConfig:
    return StablecoinConfig(
        enabled=True,
        facilitator_url="https://facilitator.example",
        public_base_url="https://commerce.example",
        network="eip155:8453",
        asset_symbol="USDC",
        asset_address=USDC,
        asset_decimals=6,
        pay_to=PAY_TO,
        quote_ttl_seconds=300,
        refund_url="https://treasury.example/refunds" if refunds else "",
    )


def _signature(quote: dict) -> str:
    now = int(time.time())
    accepted = quote["accepts"][0]
    payload = {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {
            "signature": "0x" + "a" * 130,
            "authorization": {
                "from": PAYER,
                "to": accepted["payTo"],
                "value": accepted["amount"],
                "validAfter": str(now - 5),
                "validBefore": str(now + 60),
                "nonce": "0x" + "c" * 64,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _client(
    tmp_path, facilitator: FakeFacilitator, refund_provider: FakeRefundProvider | None = None
) -> TestClient:
    return TestClient(
        create_app(
            str(tmp_path / "store.db"),
            stablecoin_config=_config(refunds=refund_provider is not None),
            stablecoin_facilitator=facilitator,
            stablecoin_refund_provider=refund_provider,
        )
    )


def _cart_and_quote(client: TestClient) -> tuple[dict[str, str], dict, str]:
    session_id = client.post("/shopping/session").json()["session_id"]
    headers = {"X-Session-Id": session_id}
    added = client.post(
        "/shopping/cart/add",
        headers=headers,
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
    )
    assert added.status_code == 200
    quote = client.post(
        "/shopping/checkout/stablecoin/quote",
        headers=headers,
        json={"shipping_address": ADDRESS, "payer_address": PAYER},
    )
    assert quote.status_code == 402
    return headers, quote.json(), quote.headers["PAYMENT-REQUIRED"]


def _complete_payment(client: TestClient) -> dict:
    headers, quote, _ = _cart_and_quote(client)
    response = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert response.status_code == 200
    return response.json()


def test_capabilities_never_imply_stablecoin_refunds_are_implemented(tmp_path):
    client = _client(tmp_path, FakeFacilitator())

    capabilities = client.get("/capabilities").json()

    assert capabilities["stablecoin_checkout"] == "available"
    assert capabilities["stablecoin_refunds"] == "deployment_integration_required"


def test_stablecoin_refund_is_digest_bound_idempotent_and_balance_limited(tmp_path):
    provider = FakeRefundProvider()
    client = _client(tmp_path, FakeFacilitator(), provider)
    payment = _complete_payment(client)
    merchant = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}

    assert client.get("/capabilities").json()["stablecoin_refunds"] == "available"
    preview = client.post(
        "/merchant/stablecoin-refunds/preview",
        headers=merchant,
        json={"payment_id": payment["payment_id"], "amount": "19.00"},
    )
    assert preview.status_code == 200
    proposal = preview.json()
    request = {
        "payment_id": payment["payment_id"],
        "amount": "19.00",
        "proposal_digest": proposal["proposal_digest"],
        "idempotency_key": "stable-refund-1",
    }
    applied = client.post("/merchant/stablecoin-refunds", headers=merchant, json=request)
    retried = client.post("/merchant/stablecoin-refunds", headers=merchant, json=request)

    assert applied.status_code == retried.status_code == 200
    assert applied.json() == retried.json()
    assert applied.json()["state"] == "completed"
    assert applied.json()["transaction_hash"] == "0x" + "d" * 64
    assert provider.calls[0][0]["to"] == PAYER
    assert provider.calls[0][0]["amount"] == "19000000"
    assert len(provider.calls) == 1

    full_preview = client.post(
        "/merchant/stablecoin-refunds/preview",
        headers=merchant,
        json={"payment_id": payment["payment_id"], "amount": "219.00"},
    ).json()
    over = client.post(
        "/merchant/stablecoin-refunds",
        headers=merchant,
        json={
            "payment_id": payment["payment_id"],
            "amount": "219.00",
            "proposal_digest": full_preview["proposal_digest"],
            "idempotency_key": "stable-refund-over",
        },
    )
    assert over.status_code == 409
    assert "unrefunded" in over.json()["detail"]


def test_uncertain_stablecoin_refund_requires_explicit_reconciliation(tmp_path):
    provider = FakeRefundProvider(uncertain=True)
    client = _client(tmp_path, FakeFacilitator(), provider)
    payment = _complete_payment(client)
    merchant = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}
    proposal = client.post(
        "/merchant/stablecoin-refunds/preview",
        headers=merchant,
        json={"payment_id": payment["payment_id"], "amount": "10.00"},
    ).json()
    response = client.post(
        "/merchant/stablecoin-refunds",
        headers=merchant,
        json={
            "payment_id": payment["payment_id"],
            "amount": "10.00",
            "proposal_digest": proposal["proposal_digest"],
            "idempotency_key": "stable-refund-uncertain",
        },
    )
    assert response.status_code == 202
    assert response.json()["state"] == "reconciliation_required"

    resolved = client.post(
        f"/merchant/stablecoin-refunds/{response.json()['refund_id']}/reconcile",
        headers=merchant,
        json={
            "resolution": "confirmed_refunded",
            "transaction_hash": "0x" + "e" * 64,
            "note": "Treasury and RPC evidence agree",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "completed"
    assert resolved.json()["transaction_hash"] == "0x" + "e" * 64


def test_x402_quote_settles_and_commits_through_the_kernel(tmp_path):
    facilitator = FakeFacilitator()
    client = _client(tmp_path, facilitator)
    headers, quote, payment_required = _cart_and_quote(client)

    challenge = json.loads(base64.b64decode(payment_required))
    assert challenge["x402Version"] == 2
    assert challenge["accepts"][0]["amount"] == "219000000"

    response = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "completed"
    assert response.json()["order_number"]
    assert response.json()["receipt"]["sealed"] is True
    settlement = json.loads(base64.b64decode(response.headers["PAYMENT-RESPONSE"]))
    assert settlement["transaction"] == "0x" + "a" * 64
    assert facilitator.verify_calls == 1
    assert facilitator.settle_calls == 1

    recovered = client.get(f"/shopping/payments/{quote['paymentId']}", headers=headers).json()
    assert recovered["order_number"] == response.json()["order_number"]
    assert recovered["receipt"]["sealed"] is True

    # A lost HTTP response is safe to retry: neither the facilitator nor the kernel
    # creates a second payment/order.
    retried = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert retried.status_code == 200
    assert retried.json()["order_number"] == response.json()["order_number"]
    assert facilitator.verify_calls == 1
    assert facilitator.settle_calls == 1


def test_cart_change_invalidates_an_immutable_payment_quote(tmp_path):
    facilitator = FakeFacilitator()
    client = _client(tmp_path, facilitator)
    headers, quote, _ = _cart_and_quote(client)
    client.post(
        "/shopping/cart/add",
        headers=headers,
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
    )

    response = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert response.status_code == 409
    assert "cart changed" in response.json()["detail"]
    assert facilitator.verify_calls == 0
    assert facilitator.settle_calls == 0


def test_a_cart_can_have_only_one_nonterminal_payment_quote(tmp_path):
    client = _client(tmp_path, FakeFacilitator())
    headers, first, _ = _cart_and_quote(client)
    duplicate = client.post(
        "/shopping/checkout/stablecoin/quote",
        headers=headers,
        json={"shipping_address": ADDRESS, "payer_address": PAYER},
    )
    assert duplicate.status_code == 409
    assert first["paymentId"] in duplicate.json()["detail"]


def test_abandoned_pre_settlement_work_is_safely_resumed(tmp_path):
    facilitator = FakeFacilitator()
    client = _client(tmp_path, facilitator)
    headers, quote, _ = _cart_and_quote(client)
    with sqlite3.connect(tmp_path / "store.db") as connection:
        connection.execute(
            "UPDATE icommerce_stablecoin_payments SET state = 'verified', updated_at = ? "
            "WHERE payment_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), quote["paymentId"]),
        )

    response = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "completed"
    assert facilitator.settle_calls == 1


def test_abandoned_settlement_moves_to_the_operator_recovery_queue(tmp_path):
    client = _client(tmp_path, FakeFacilitator())
    _, quote, _ = _cart_and_quote(client)
    with sqlite3.connect(tmp_path / "store.db") as connection:
        connection.execute(
            "UPDATE icommerce_stablecoin_payments SET state = 'settling', updated_at = ? "
            "WHERE payment_id = ?",
            ((datetime.now(UTC) - timedelta(minutes=2)).isoformat(), quote["paymentId"]),
        )

    merchant = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}
    queue = client.get("/merchant/stablecoin-payments", headers=merchant)
    payment = next(
        item for item in queue.json()["payments"] if item["payment_id"] == quote["paymentId"]
    )
    assert payment["state"] == "reconciliation_required"
    assert "unknown external outcome" in payment["last_error"]


def test_unknown_settlement_is_never_blindly_retried(tmp_path):
    facilitator = FakeFacilitator(settle_uncertain=True)
    client = _client(tmp_path, facilitator)
    headers, quote, _ = _cart_and_quote(client)

    first = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert first.status_code == 202
    assert first.json()["state"] == "reconciliation_required"
    assert facilitator.settle_calls == 1

    second = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert second.status_code == 409
    assert facilitator.settle_calls == 1

    merchant = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}
    queue = client.get("/merchant/stablecoin-payments", headers=merchant)
    assert queue.status_code == 200
    assert quote["paymentId"] in {payment["payment_id"] for payment in queue.json()["payments"]}
    reconciled = client.post(
        f"/merchant/stablecoin-payments/{quote['paymentId']}/reconcile",
        headers=merchant,
        json={
            "resolution": "confirmed_settled",
            "transaction_hash": "0x" + "b" * 64,
            "note": "Verified against provider and Base RPC.",
        },
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["state"] == "completed"
    assert reconciled.json()["order_number"]
    assert facilitator.settle_calls == 1


def test_payment_status_is_scoped_to_the_shopping_session(tmp_path):
    client = _client(tmp_path, FakeFacilitator())
    _, quote, _ = _cart_and_quote(client)
    other = {"X-Session-Id": client.post("/shopping/session").json()["session_id"]}
    response = client.get(f"/shopping/payments/{quote['paymentId']}", headers=other)
    assert response.status_code == 404


def test_invalid_facilitator_payer_does_not_settle(tmp_path):
    facilitator = FakeFacilitator(payer="0x4444444444444444444444444444444444444444")
    client = _client(tmp_path, facilitator)
    headers, quote, _ = _cart_and_quote(client)
    response = client.post(
        f"/shopping/checkout/stablecoin/{quote['paymentId']}",
        headers={**headers, "PAYMENT-SIGNATURE": _signature(quote)},
        json={"quote_digest": quote["quoteDigest"]},
    )
    assert response.status_code == 409
    assert facilitator.verify_calls == 1
    assert facilitator.settle_calls == 0


@pytest.mark.parametrize(
    ("operation", "response", "expected"),
    [
        ("verify", {"isValid": True, "payer": PAYER}, True),
        (
            "settle",
            {"success": True, "payer": PAYER, "transaction": "0x" + "d" * 64},
            True,
        ),
    ],
)
async def test_http_facilitator_uses_the_x402_v2_contract(operation, response, expected):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    facilitator = HttpX402Facilitator(_config(), http=http)
    payload = {"x402Version": 2, "payload": {"signature": "0x" + "a" * 130}}
    requirement = {"scheme": "exact", "network": "eip155:8453", "amount": "10"}
    result = await getattr(facilitator, operation)(payload, requirement)

    assert result.success is expected
    assert seen[0].url == f"https://facilitator.example/{operation}"
    assert seen[0].headers["user-agent"] == "icommerce-agents/x402-v2"
    assert json.loads(seen[0].content) == {
        "x402Version": 2,
        "paymentPayload": payload,
        "paymentRequirements": requirement,
    }
    await facilitator.aclose()
    assert not http.is_closed, "an injected shared client must remain owned by its caller"
    await http.aclose()


async def test_http_facilitator_treats_timeout_and_invalid_json_as_uncertain():
    outcomes = [
        httpx.ConnectTimeout("timeout"),
        httpx.Response(200, content=b"not json"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        facilitator = HttpX402Facilitator(_config(), http=http)
        for operation in ("verify", "settle"):
            with pytest.raises(FacilitatorUncertain, match=operation):
                await getattr(facilitator, operation)({}, {})


async def test_http_refund_provider_binds_idempotency_and_keeps_credentials_server_side():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "transaction": "0x" + "f" * 64,
                "network": "eip155:8453",
            },
        )

    config = StablecoinConfig(
        **{
            **_config(refunds=True).__dict__,
            "refund_bearer_token": "server-secret",
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = HttpStablecoinRefundProvider(config, http=http)
        result = await provider.refund({"refundId": "rfnd_1"}, "refund-key-1")

    assert result.success is True
    assert seen[0].url == "https://treasury.example/refunds"
    assert seen[0].headers["idempotency-key"] == "refund-key-1"
    assert seen[0].headers["authorization"] == "Bearer server-secret"
    assert json.loads(seen[0].content) == {"refundId": "rfnd_1"}


def test_manual_payment_reconciliation_requires_a_dedicated_permission(tmp_path):
    auth = AuthConfig(
        mode="jwt",
        issuer="https://identity.example.test",
        audience="icommerce-host",
        hs256_secret=AUTH_SECRET,
    )
    client = TestClient(
        create_app(
            str(tmp_path / "store.db"),
            auth_config=auth,
            stablecoin_config=_config(),
            stablecoin_facilitator=FakeFacilitator(),
        )
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": auth.issuer,
            "aud": auth.audience,
            "sub": "operator:payments",
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
        "/merchant/stablecoin-payments/missing/reconcile",
        headers=headers,
        json={"resolution": "confirmed_not_settled", "note": "Checked provider logs."},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "payment reconciliation access required"
