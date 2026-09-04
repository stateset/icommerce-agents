from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from engine_backend.store import EngineStore
from host.sessions import ChatTurnBusy, SessionRegistry


class ExampleState(BaseModel):
    seen: list[str] = []


async def test_chat_transcript_and_provenance_survive_worker_change(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first_store = EngineStore(db_path)
    first_store.bind("session-1", "customer-1", "customer")
    first = SessionRegistry(ExampleState, first_store, "shopping", 60)
    first.start("session-1")
    claimed = await first.claim("session-1")
    claimed.session.state.seen.append("SKU-1")
    claimed.session.messages.append({"role": "user", "content": "show SKU-1"})
    await first.finish(claimed)

    second_store = EngineStore(db_path)
    second = SessionRegistry(ExampleState, second_store, "shopping", 60)
    recovered = await second.claim("session-1")
    assert recovered.session.state.seen == ["SKU-1"]
    assert recovered.session.messages == [{"role": "user", "content": "show SKU-1"}]
    await second.finish(recovered)


async def test_only_one_worker_can_own_a_chat_turn(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first_store = EngineStore(db_path)
    first_store.bind("session-1", "customer-1", "customer")
    first = SessionRegistry(ExampleState, first_store, "shopping", 60)
    second = SessionRegistry(ExampleState, EngineStore(db_path), "shopping", 60)
    first.start("session-1")

    claimed = await first.claim("session-1")
    with pytest.raises(ChatTurnBusy):
        await second.claim("session-1")
    await first.finish(claimed)

    next_turn = await second.claim("session-1")
    await second.finish(next_turn)


async def test_expired_chat_lease_is_recoverable(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    store = EngineStore(db_path)
    store.bind("session-1", "customer-1", "customer")
    first = SessionRegistry(ExampleState, store, "shopping", 60)
    first.start("session-1")
    abandoned = await first.claim("session-1")

    connection = store._control_connection()
    try:
        connection.execute(
            "UPDATE icommerce_agent_chat_sessions SET lease_expires_at = ? "
            "WHERE session_id = ? AND role = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "session-1", "shopping"),
        )
        connection.commit()
    finally:
        connection.close()

    recovered = SessionRegistry(ExampleState, EngineStore(db_path), "shopping", 60)
    claimed = await recovered.claim("session-1")
    assert claimed.owner != abandoned.owner
    await recovered.finish(claimed)


def test_rate_limit_is_atomic_across_store_instances(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first = EngineStore(db_path)
    second = EngineStore(db_path)

    assert first.consume_rate_limit("shopping:customer-1", 2, 1_000)
    assert second.consume_rate_limit("shopping:customer-1", 2, 1_000)
    assert not first.consume_rate_limit("shopping:customer-1", 2, 1_000)
    assert second.consume_rate_limit("merchant:customer-1", 2, 1_000)


def test_expired_session_cleanup_cascades_chat_and_cart_state(tmp_path):
    store = EngineStore(str(tmp_path / "sessions.db"))
    store.bind(
        "expired-session",
        "customer-1",
        "customer",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    registry = SessionRegistry(ExampleState, store, "shopping", 60)
    registry.start("expired-session")
    store.claim_session_cart("expired-session", "cart-1")

    assert store.cleanup_expired_sessions() == 1
    connection = store._control_connection()
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM icommerce_agent_chat_sessions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM icommerce_agent_session_carts").fetchone()[0]
            == 0
        )
    finally:
        connection.close()
