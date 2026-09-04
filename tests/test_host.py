import sqlite3

from fastapi.testclient import TestClient

from host.app import create_app

FAKE_PROPOSAL = {"proposal_digest": "sha256:" + "0" * 64}


def _approval_json(store, change_id):
    import asyncio

    from engine_backend.staging import load_record

    return {"proposal_digest": asyncio.run(load_record(store, change_id))["proposal_digest"]}


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


def test_readiness_proves_the_engine_can_answer(tmp_path):
    response = client(tmp_path).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


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
    response = c.post(
        "/merchant/changes/chg-nope/approve",
        headers={"X-Session-Id": session_id},
        json=FAKE_PROPOSAL,
    )
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


def test_session_end_revokes_the_binding_and_role_state(tmp_path):
    c = TestClient(create_app(str(tmp_path / "store.db")))
    shopping = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    merchant = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}

    assert c.post("/shopping/session/end", headers=shopping).json() == {"status": "ended"}
    assert c.get("/shopping/cart", headers=shopping).status_code == 401
    assert c.post("/shopping/session/end", headers=shopping).status_code == 401

    assert c.post("/merchant/session/end", headers=merchant).json() == {"status": "ended"}
    assert c.get("/merchant/changes", headers=merchant).status_code == 401


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
            json=FAKE_PROPOSAL,
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


def test_http_write_inputs_are_bounded_before_reaching_the_engine_or_model(tmp_path):
    c = client(tmp_path)
    shopping = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    merchant = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}

    for quantity in (0, -1, 25):
        response = c.post(
            "/shopping/cart/add",
            json={"product_id": "TENT-RIDGE-GRN", "quantity": quantity},
            headers=shopping,
        )
        assert response.status_code == 422
    assert c.post("/shopping/chat", json={"message": ""}, headers=shopping).status_code == 422
    assert (
        c.post("/merchant/chat", json={"message": "x" * 20_001}, headers=merchant).status_code
        == 422
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
    assert (
        c.post(
            "/merchant/changes/chg-nope/approve", headers=shopping, json=FAKE_PROPOSAL
        ).status_code
        == 401
    )

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
    approval = _approval_json(store, change.change_id)
    assert c.post(approve, headers=headers, json=approval).status_code == 200

    asyncio.run(staging.save(store, change.model_copy(update={"status": ChangeStatus.APPLIED})))
    response = c.post(approve, headers=headers, json=approval)
    assert response.status_code == 409
    assert "applied" in response.json()["detail"]


def test_http_approval_reaches_both_executor_and_backend_and_is_consumed(tmp_path, monkeypatch):
    """Claude Commerce checks session state before dispatch; the adapter checks its
    own operator-bound mark at the mutation boundary. The route must populate both."""
    import asyncio

    from merchant_agent.types import PriceUpdateItem

    import host.app as host_app
    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path))
    headers = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}

    external_store = EngineStore(db_path)
    backend = EngineMerchant(
        external_store,
        KernelClient(
            external_store,
            host_app.CONFIG_DIR / "kernel-policy.json",
            host_app.CONFIG_DIR / "kernel-principal.json",
        ),
    )
    operator = "user:acme-operator"
    change = asyncio.run(
        backend.stage_price_update(
            host_app.MerchantSessionContext(
                session_id="external", merchant_id="store:acme", operator=operator
            ),
            [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)],
        )
    )

    seen = {}

    async def apply_from_fake_turn(agent, messages, session, state):
        del messages
        seen["state"] = state
        seen["approved_before_dispatch"] = change.change_id in state.approved_change_ids
        seen["applied"] = await agent.backend.apply_change(session, change.change_id)
        if False:  # keep this an async generator, matching stream_turn's contract
            yield

    monkeypatch.setattr(host_app.MerchantAgent, "stream_turn", apply_from_fake_turn)

    assert (
        c.post(
            f"/merchant/changes/{change.change_id}/approve",
            headers=headers,
            json=_approval_json(external_store, change.change_id),
        ).status_code
        == 200
    )
    assert (
        c.post("/merchant/chat", json={"message": "apply it"}, headers=headers).status_code == 200
    )

    assert seen["approved_before_dispatch"] is True
    assert seen["applied"].status is host_app.ChangeStatus.APPLIED
    assert change.change_id not in seen["state"].approved_change_ids


def test_capabilities_reports_presence_never_validity(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = client(tmp_path)
    body = c.get("/capabilities").json()
    assert body == {
        "assistant": "unconfigured",
        "stablecoin_checkout": "disabled",
        "direct_checkout": "available",
    }
    assert "key" not in str(body).lower()


def test_shopping_cart_read_matches_what_was_added(tmp_path):
    c = client(tmp_path)
    headers = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}
    c.post(
        "/shopping/cart/add",
        json={"product_id": "TENT-RIDGE-GRN", "quantity": 1},
        headers=headers,
    )
    read_back = c.get("/shopping/cart", headers=headers).json()
    assert [item["product_id"] for item in read_back["items"]] == ["TENT-RIDGE-GRN"]
    assert read_back["grand_total_exact"]


def test_shopping_cart_read_on_a_fresh_session_creates_no_cart(tmp_path):
    """A GET is a read: a session that never called ``cart/add`` must not cause a cart
    row to appear in the engine's own store just from being read."""
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path))
    headers = {"X-Session-Id": c.post("/shopping/session").json()["session_id"]}

    response = c.get("/shopping/cart", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"] == []

    store = EngineStore(db_path)
    assert store.commerce.carts.list() == []


def test_shopping_cart_read_requires_a_session(tmp_path):
    assert client(tmp_path).get("/shopping/cart").status_code == 401


def test_shopping_orders_read_requires_a_session(tmp_path):
    assert client(tmp_path).get("/shopping/orders").status_code == 401


def test_merchant_changes_read_requires_a_session(tmp_path):
    assert client(tmp_path).get("/merchant/changes").status_code == 401


def test_merchant_changes_read_excludes_discarded_changes(tmp_path):
    """`GET /merchant/changes` reports pending and applied changes -- a discarded one
    has nothing left to show and must not appear."""
    import asyncio

    from merchant_agent.types import ActorKind, ChangeItem, ChangeKind

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
    asyncio.run(staging.discard(store, change.change_id, "user:acme-operator", ActorKind.OPERATOR))

    changes = c.get("/merchant/changes", headers=headers).json()["changes"]
    assert change.change_id not in {item["change_id"] for item in changes}


def test_merchant_changes_exposes_durable_apply_control_state(tmp_path):
    import asyncio

    from merchant_agent.types import ChangeItem, ChangeKind

    from engine_backend import staging
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path))
    headers = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}
    store = EngineStore(db_path)
    change = staging.new_change(
        ChangeKind.PRICE_UPDATE,
        "Update price",
        [ChangeItem(target="TENT-RIDGE-TAN", field="price", before="219.00", after="199.00")],
        "user:acme-operator",
    )
    asyncio.run(staging.save(store, change))
    mismatch = c.post(
        f"/merchant/changes/{change.change_id}/approve",
        headers=headers,
        json=FAKE_PROPOSAL,
    )
    assert mismatch.status_code == 409
    assert (
        c.post(
            f"/merchant/changes/{change.change_id}/approve",
            headers=headers,
            json=_approval_json(store, change.change_id),
        ).status_code
        == 200
    )

    records = c.get("/merchant/changes", headers=headers).json()["changes"]
    item = next(record for record in records if record["change_id"] == change.change_id)
    assert item["apply_control"]["state"] == "approved"
    assert item["apply_control"]["approved_by"] == "user:acme-operator"
    assert item["apply_control"]["approved_at"]


def test_operator_can_reconcile_an_ambiguous_apply_from_observed_state(tmp_path):
    import asyncio
    from pathlib import Path

    from merchant_agent.types import ChangeStatus, MerchantSessionContext, PriceUpdateItem

    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.staging import load, load_record
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    client = TestClient(create_app(db_path))
    headers = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}
    store = EngineStore(db_path)
    backend = EngineMerchant(
        store,
        KernelClient(
            store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    session = MerchantSessionContext(
        session_id="outside", merchant_id="acme", operator="user:acme-operator"
    )
    change = asyncio.run(
        backend.stage_price_update(
            session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        )
    )
    backend.approve(change.change_id, session.operator)
    record = asyncio.run(load_record(store, change.change_id))
    digest = record["proposal_digest"]
    claim = store.claim_approval(change.change_id, session.operator, digest, ["TENT-RIDGE-TAN"])
    asyncio.run(
        store.write_sql(
            "UPDATE product_variants SET price = ?, version = version + 1 WHERE sku = ?",
            ("199.00", "TENT-RIDGE-TAN"),
        )
    )
    store.finish_approval_attempt(
        change.change_id,
        claim.attempt_id,
        outcome="reconciliation_required",
        error="simulated response loss",
    )

    detail = client.get(f"/merchant/changes/{change.change_id}/reconciliation", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["assessment"]["outcome"] == "applied"
    assert [event["event"] for event in detail.json()["events"]] == [
        "approved",
        "claimed",
        "reconciliation_required",
    ]

    resolved = client.post(
        f"/merchant/changes/{change.change_id}/reconciliation",
        headers=headers,
        json={"proposal_digest": digest, "resolution": "confirmed_applied"},
    )
    assert resolved.status_code == 200
    assert asyncio.run(load(store, change.change_id)).status is ChangeStatus.APPLIED
    control = store.approval_record(change.change_id)
    assert control["state"] == "resolved"
    assert control["resolution"] == "confirmed_applied"
    assert store.approval_events(change.change_id)[-1]["event"] == ("reconciled:confirmed_applied")


def test_reconciliation_cannot_confirm_a_write_that_did_not_land(tmp_path):
    import asyncio
    from pathlib import Path

    from merchant_agent.types import MerchantSessionContext, PriceUpdateItem

    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.staging import load_record
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    client = TestClient(create_app(db_path))
    headers = {"X-Session-Id": client.post("/merchant/session").json()["session_id"]}
    store = EngineStore(db_path)
    backend = EngineMerchant(
        store,
        KernelClient(
            store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    session = MerchantSessionContext(
        session_id="outside", merchant_id="acme", operator="user:acme-operator"
    )
    change = asyncio.run(
        backend.stage_price_update(
            session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        )
    )
    backend.approve(change.change_id, session.operator)
    digest = asyncio.run(load_record(store, change.change_id))["proposal_digest"]
    claim = store.claim_approval(change.change_id, session.operator, digest, ["TENT-RIDGE-TAN"])
    store.finish_approval_attempt(
        change.change_id,
        claim.attempt_id,
        outcome="reconciliation_required",
        error="simulated failure before write",
    )

    refused = client.post(
        f"/merchant/changes/{change.change_id}/reconciliation",
        headers=headers,
        json={"proposal_digest": digest, "resolution": "confirmed_applied"},
    )
    assert refused.status_code == 409
    accepted = client.post(
        f"/merchant/changes/{change.change_id}/reconciliation",
        headers=headers,
        json={"proposal_digest": digest, "resolution": "accepted_current_state"},
    )
    assert accepted.status_code == 200
    control = store.approval_record(change.change_id)
    assert control["state"] == "resolved"
    assert control["resolution"] == "accepted_current_state"


def test_operator_can_recover_a_stale_applying_claim_without_retrying_it(tmp_path):
    import asyncio
    from pathlib import Path

    from merchant_agent.types import MerchantSessionContext, PriceUpdateItem

    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.staging import load_record
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path, stale_apply_seconds=1))
    headers = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}
    store = EngineStore(db_path)
    backend = EngineMerchant(
        store,
        KernelClient(
            store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    session = MerchantSessionContext(
        session_id="outside", merchant_id="store:acme", operator="user:acme-operator"
    )
    change = asyncio.run(
        backend.stage_price_update(
            session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        )
    )
    backend.approve(change.change_id, session.operator)
    digest = asyncio.run(load_record(store, change.change_id))["proposal_digest"]
    claim = store.claim_approval(change.change_id, session.operator, digest, ["TENT-RIDGE-TAN"])
    assert claim.attempt_id

    endpoint = f"/merchant/changes/{change.change_id}/reconciliation/start"
    too_soon = c.post(endpoint, headers=headers, json={"proposal_digest": digest})
    assert too_soon.status_code == 409
    assert "still within its lease" in too_soon.json()["detail"]

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE icommerce_agent_approvals SET claimed_at = ? WHERE change_id = ?",
        ("2000-01-01T00:00:00+00:00", change.change_id),
    )
    connection.commit()
    connection.close()

    recovered = c.post(endpoint, headers=headers, json={"proposal_digest": digest})
    assert recovered.status_code == 200
    assert recovered.json()["assessment"]["outcome"] == "not_applied"
    assert store.approval_record(change.change_id)["state"] == "reconciliation_required"
    connection = sqlite3.connect(db_path)
    lease = connection.execute(
        "SELECT change_id FROM icommerce_agent_target_leases WHERE target = ?",
        ("TENT-RIDGE-TAN",),
    ).fetchone()
    connection.close()
    assert lease == (change.change_id,)
    assert store.approval_events(change.change_id)[-1]["event"] == "reconciliation_required"


def test_discarded_lifecycle_remains_visible_while_reconciliation_is_in_flight(tmp_path):
    import asyncio
    from pathlib import Path

    from merchant_agent.types import (
        ActorKind,
        ChangeStatus,
        MerchantSessionContext,
        PriceUpdateItem,
    )

    from engine_backend import staging
    from engine_backend.kernel import KernelClient
    from engine_backend.merchant import EngineMerchant
    from engine_backend.store import EngineStore

    db_path = str(tmp_path / "store.db")
    c = TestClient(create_app(db_path))
    headers = {"X-Session-Id": c.post("/merchant/session").json()["session_id"]}
    store = EngineStore(db_path)
    backend = EngineMerchant(
        store,
        KernelClient(
            store, Path("config/kernel-policy.json"), Path("config/kernel-principal.json")
        ),
    )
    session = MerchantSessionContext(
        session_id="outside", merchant_id="store:acme", operator="user:acme-operator"
    )
    change = asyncio.run(
        backend.stage_price_update(
            session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
        )
    )
    backend.approve(change.change_id, session.operator)
    record = asyncio.run(staging.load_record(store, change.change_id))
    digest = record["proposal_digest"]
    claim = store.claim_approval(change.change_id, session.operator, digest, ["TENT-RIDGE-TAN"])
    store.finish_approval_attempt(
        change.change_id,
        claim.attempt_id,
        outcome="reconciliation_required",
        error="ambiguous",
    )
    store.claim_reconciliation(change.change_id, session.operator, digest, "accepted_current_state")
    asyncio.run(
        staging.save(
            store,
            change.model_copy(
                update={
                    "status": ChangeStatus.DISCARDED,
                    "discarded_by": session.operator,
                    "discarded_by_kind": ActorKind.OPERATOR,
                }
            ),
        )
    )

    records = c.get("/merchant/changes", headers=headers).json()["changes"]
    visible = next(item for item in records if item["change_id"] == change.change_id)
    assert visible["status"] == "discarded"
    assert visible["apply_control"]["state"] == "reconciling"
    assert visible["recovery_available_at"]

    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE icommerce_agent_approvals SET resolved_at = ? WHERE change_id = ?",
        ("2000-01-01T00:00:00+00:00", change.change_id),
    )
    connection.commit()
    connection.close()
    started = c.post(
        f"/merchant/changes/{change.change_id}/reconciliation/start",
        headers=headers,
        json={"proposal_digest": digest},
    )
    assert started.status_code == 200
    resolved = c.post(
        f"/merchant/changes/{change.change_id}/reconciliation",
        headers=headers,
        json={"proposal_digest": digest, "resolution": "accepted_current_state"},
    )
    assert resolved.status_code == 200
    assert store.approval_record(change.change_id)["state"] == "resolved"
