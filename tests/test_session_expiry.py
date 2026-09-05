"""Session lifetimes are instants, not lexicographically ordered local timestamps."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from engine_backend.store import EngineStore, PrincipalBinding

NOW = datetime(2026, 9, 5, 0, 0, 0, 123456, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("engine_backend.store.datetime", FrozenDatetime)
    return NOW


@pytest.fixture(params=[False, True], ids=["durable", "memory"])
def store(tmp_path, request):
    return EngineStore(":memory:" if request.param else str(tmp_path / "sessions.db"))


def test_expiry_is_normalized_and_compared_as_an_instant(store, clock):
    for minutes in (-720, -480, -210, 0, 330, 345, 840):
        zone = timezone(timedelta(minutes=minutes))
        for label, delta in (("past", -1), ("now", 0), ("future", 1)):
            instant = clock + timedelta(microseconds=delta)
            binding = store.bind(
                f"{minutes}:{label}", "customer", "customer", expires_at=instant.astimezone(zone)
            )
            assert binding.expires_at == instant
            assert binding.expires_at.tzinfo == UTC
    assert store.cleanup_expired_sessions() == 14
    for minutes in (-720, -480, -210, 0, 330, 345, 840):
        with pytest.raises(KeyError):
            store.binding(f"{minutes}:past")
        with pytest.raises(KeyError):
            store.binding(f"{minutes}:now")
        assert store.binding(f"{minutes}:future").expires_at > clock
    assert store.cleanup_expired_sessions() == 0


def test_naive_expiry_cannot_create_or_overwrite_binding(store, clock):
    original = store.bind("existing", "customer", "customer", expires_at=clock + timedelta(hours=1))
    for session_id in ("new", "existing"):
        with pytest.raises(ValidationError, match="timezone"):
            store.bind(session_id, "customer", "customer", expires_at=clock.replace(tzinfo=None))
    with pytest.raises(KeyError):
        store.binding("new")
    assert store.binding("existing") == original


def test_principal_snapshots_cannot_be_mutated(store, clock):
    original = store.bind("session", "customer", "customer", expires_at=clock + timedelta(hours=1))
    for binding in (original, store.binding("session")):
        for field, value in (
            ("subject_id", "intruder"),
            ("kind", "operator"),
            ("expires_at", None),
        ):
            with pytest.raises(ValidationError, match="frozen"):
                setattr(binding, field, value)
    assert store.binding("session") == original


def _legacy_expiry(store, session_id, expiry):
    store.bind(session_id, "customer", "customer")
    store.claim_session_cart(session_id, f"cart:{session_id}")
    connection = store._control_connection()
    try:
        connection.execute(
            "UPDATE icommerce_agent_sessions SET expires_at = ? WHERE session_id = ?",
            (expiry, session_id),
        )
        connection.commit()
    finally:
        connection.close()


def test_legacy_offsets_cleanup_preserves_future_cart_and_exact_boundary(tmp_path, clock):
    store = EngineStore(str(tmp_path / "sessions.db"))
    for minutes in (-720, 0, 330, 840):
        for label, delta in (("past", -1), ("now", 0), ("future", 1)):
            expiry = (clock + timedelta(microseconds=delta)).astimezone(
                timezone(timedelta(minutes=minutes))
            )
            _legacy_expiry(store, f"{minutes}:{label}", expiry.isoformat())
    assert store.cleanup_expired_sessions() == 8
    for minutes in (-720, 0, 330, 840):
        for label in ("past", "now"):
            assert store.session_cart_id(f"{minutes}:{label}") is None
        assert store.binding(f"{minutes}:future").expires_at > clock
        assert store.session_cart_id(f"{minutes}:future") == f"cart:{minutes}:future"


@pytest.mark.parametrize("expiry", ["not-a-date", "2026-09-04T23:59:00"])
def test_ambiguous_legacy_expiry_denies_access_without_destroying_state(tmp_path, clock, expiry):
    store = EngineStore(str(tmp_path / "sessions.db"))
    _legacy_expiry(store, "session", expiry)
    with pytest.raises(KeyError):
        store.binding("session")
    assert store.cleanup_expired_sessions() == 0
    assert store.session_cart_id("session") == "cart:session"


def test_bulk_cleanup_does_not_delete_concurrent_renewal(tmp_path, monkeypatch, clock):
    path = str(tmp_path / "sessions.db")
    first, second = EngineStore(path), EngineStore(path)
    _legacy_expiry(first, "session", (clock - timedelta(hours=1)).isoformat())
    validate = PrincipalBinding.model_validate

    def renew_after_selection(row):
        second.bind("session", "customer", "customer", expires_at=clock + timedelta(hours=1))
        return validate(row)

    with monkeypatch.context() as patch:
        patch.setattr(PrincipalBinding, "model_validate", renew_after_selection)
        assert first.cleanup_expired_sessions() == 0
    assert first.binding("session").expires_at > clock
    assert first.session_cart_id("session") == "cart:session"
