"""A stopped process is not a dead process, even after its database lease expires."""

import multiprocessing
import os
import signal
from datetime import UTC, datetime, timedelta

from engine_backend.store import EngineStore
from engine_backend.turn_locks import TurnLocks


def _hold_turn(db_path, ready):
    store = EngineStore(db_path)
    claimed = store.claim_chat_turn("session", "shopping", 60)
    ready.send(claimed[0])
    signal.pause()


def test_paused_worker_cannot_be_replaced_but_dead_worker_can(tmp_path):
    db_path = str(tmp_path / "store.db")
    store = EngineStore(db_path)
    store.bind("session", "customer", "customer")
    store.initialize_chat_session("session", "shopping", "{}", "[]")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_turn, args=(db_path, child))
    process.start()
    try:
        assert parent.poll(20), "worker did not claim the turn"
        owner = parent.recv()
        os.kill(process.pid, signal.SIGSTOP)
        connection = store._control_connection()
        try:
            connection.execute(
                "UPDATE icommerce_agent_chat_sessions SET lease_expires_at = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
            )
            connection.commit()
        finally:
            connection.close()

        assert store.claim_chat_turn("session", "shopping", 60) is None
        process.kill()
        process.join(10)
        assert not process.is_alive()
        recovered = store.claim_chat_turn("session", "shopping", 60)
        assert recovered is not None
        assert recovered[0] != owner
        store.finish_chat_turn("session", "shopping", recovered[0], "{}", "[]")
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()


def test_locks_isolate_roles_sessions_and_exact_owners(tmp_path):
    first = TurnLocks(str(tmp_path / "store.db"))
    second = TurnLocks(str(tmp_path / "store.db"))
    assert first.acquire("session", "shopping", "a")
    try:
        assert not second.acquire("session", "shopping", "b")
        first.release("wrong-owner")
        assert not second.acquire("session", "shopping", "b")
        assert second.acquire("session", "merchant", "c")
        assert second.acquire("different-session", "shopping", "d")
    finally:
        first.release("a")
        second.release("c")
        second.release("d")
    assert second.acquire("session", "shopping", "e")
    second.release("e")
    assert all(len(path.name) == 64 for path in first.directory.iterdir())
