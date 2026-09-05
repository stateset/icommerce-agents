from datetime import UTC, datetime, timedelta

import pytest

from engine_backend.store import EngineStore, PrincipalBinding


@pytest.mark.parametrize("changed", ["subject", "role", "authenticated_subject", "store"])
def test_existing_session_identity_cannot_be_reassigned(engine_db, changed):
    path = engine_db("store.db")
    store = EngineStore(path)
    original = store.bind("session", "customer", "customer", authenticated_subject="oidc-subject")
    other = EngineStore(path)
    if changed == "store":
        other.store_id = "different-store"
    with pytest.raises(ValueError, match="cannot be rebound"):
        other.bind(
            "session",
            "different" if changed == "subject" else "customer",
            "operator" if changed == "role" else "customer",
            authenticated_subject=None if changed == "authenticated_subject" else "oidc-subject",
        )
    assert store.binding("session") == original


def test_same_identity_can_reconnect_but_cannot_inherit_another_customers_cart(engine_db):
    path = engine_db("store.db")
    first = EngineStore(path)
    first.bind("mcp-shopping", "customer-1", "customer")
    first.claim_session_cart("mcp-shopping", "customer-1-cart")
    second = EngineStore(path)
    second.bind("mcp-shopping", "customer-1", "customer")
    with pytest.raises(ValueError, match="cannot be rebound"):
        second.bind("mcp-shopping", "customer-2", "customer")
    assert second.binding("mcp-shopping").subject_id == "customer-1"


def test_durable_identity_reads_do_not_accumulate_worker_cache(engine_db):
    store = EngineStore(engine_db("store.db"))
    for index in range(25):
        session_id = f"session-{index}"
        original = store.bind(session_id, f"customer-{index}", "customer")
        assert store.binding(session_id) == original
    assert not hasattr(store, "_bindings")
    assert store.cleanup_expired_sessions() == 0
    assert store.binding("session-0").subject_id == "customer-0"


@pytest.mark.parametrize("renewal_has_expiry", [True, False])
def test_expired_snapshot_cannot_delete_cross_worker_renewal(
    engine_db, monkeypatch, renewal_has_expiry
):
    path = engine_db("store.db")
    reader = EngineStore(path)
    writer = EngineStore(path)
    reader.bind(
        "session", "customer", "customer", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    reader.claim_session_cart("session", "preserved-cart")
    new_expiry = datetime.now(UTC) + timedelta(hours=1) if renewal_has_expiry else None
    validate = PrincipalBinding.model_validate

    def renew_after_read(row):
        # Deterministically interleave a second worker after SELECT has returned
        # the expired snapshot, but before the reader attempts expiry cleanup.
        writer.bind("session", "customer", "customer", expires_at=new_expiry)
        return validate(row)

    with monkeypatch.context() as patch:
        patch.setattr(PrincipalBinding, "model_validate", renew_after_read)
        with pytest.raises(KeyError):
            reader.binding("session")

    assert reader.binding("session").expires_at == new_expiry
    assert reader.session_cart_id("session") == "preserved-cart"


def test_expiry_cleanup_preserves_current_and_nonexpiring_bindings(engine_db):
    store = EngineStore(engine_db("store.db"))
    now = datetime.now(UTC)
    store.bind("expired", "customer", "customer", expires_at=now - timedelta(seconds=1))
    store.bind("current", "customer", "customer", expires_at=now + timedelta(hours=1))
    store.bind("permanent", "operator", "operator")
    assert store.cleanup_expired_sessions() == 1
    assert store.cleanup_expired_sessions() == 0
    with pytest.raises(KeyError):
        store.binding("expired")
    assert store.binding("current").subject_id == "customer"
    assert store.binding("permanent").kind == "operator"
    store.unbind("current")
    with pytest.raises(KeyError):
        store.binding("current")
