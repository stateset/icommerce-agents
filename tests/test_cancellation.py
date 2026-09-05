"""Disconnects must not outlive the locks and leases protecting engine work."""

import asyncio
import threading

import anyio
import pytest
from pydantic import BaseModel

from engine_backend.async_utils import complete_before_cancelling
from engine_backend.store import EngineStore
from host.sessions import SessionRegistry


class ExampleState(BaseModel):
    seen: list[str] = []


@pytest.mark.parametrize("operation", ["write", "call", "sql"])
async def test_cancelled_engine_work_keeps_outer_lock_until_thread_exits(
    engine_db, monkeypatch, operation
):
    store = EngineStore(engine_db("store.db"))
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    second_entered = asyncio.Event()

    def blocking(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "test failed to release worker"

    if operation == "sql":
        # Block the real SQL connection factory before the UPDATE is executed.
        import engine_backend.store as store_module

        connect = store_module.sqlite3.connect

        def blocking_connect(*args, **kwargs):
            blocking()
            return connect(*args, **kwargs)

        monkeypatch.setattr(store_module.sqlite3, "connect", blocking_connect)

    async def first():
        async with store.serialized("outer"):
            if operation == "write":
                await store.write("inner", blocking)
            elif operation == "call":
                await store.call(blocking)
            else:
                await store.write_sql("UPDATE products SET name = ? WHERE id = ?", ("x", "x"))

    async def second():
        async with store.serialized("outer"):
            second_entered.set()

    task = asyncio.create_task(first())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    contender = asyncio.create_task(second())
    try:
        # A second cancellation must not break the cleanup wait either.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_entered.wait(), 0.05)
        assert not task.done()
    finally:
        release.set()
        results = await asyncio.gather(task, contender, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1] is None
    assert second_entered.is_set()


async def test_cancelled_chat_claim_releases_late_acquired_lease(engine_db, monkeypatch):
    store = EngineStore(engine_db("sessions.db"))
    store.bind("session", "customer", "customer")
    registry = SessionRegistry(ExampleState, store, "shopping", 60)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = store.claim_chat_turn

    def delayed_claim(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(store, "claim_chat_turn", delayed_claim)
    task = asyncio.create_task(registry.claim("session"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    monkeypatch.setattr(store, "claim_chat_turn", original)
    claimed = await registry.claim("session")
    await registry.finish(claimed)


async def test_failed_claim_does_not_block_the_next_claim(engine_db, monkeypatch):
    store = EngineStore(engine_db("sessions.db"))
    store.bind("session", "customer", "customer")
    registry = SessionRegistry(ExampleState, store, "shopping", 60)
    original = store.claim_chat_turn

    def fail(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "claim_chat_turn", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await registry.claim("session")
    monkeypatch.setattr(store, "claim_chat_turn", original)
    claimed = await registry.claim("session")
    await registry.finish(claimed)


async def test_cancelled_finish_persists_state_before_releasing_lease(engine_db, monkeypatch):
    store = EngineStore(engine_db("sessions.db"))
    store.bind("session", "customer", "customer")
    registry = SessionRegistry(ExampleState, store, "shopping", 60)
    claimed = await registry.claim("session")
    claimed.session.state.seen.append("SKU-1")
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    original = store.finish_chat_turn

    def delayed_finish(*args):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(store, "finish_chat_turn", delayed_finish)
    task = asyncio.create_task(registry.finish(claimed))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    recovered = SessionRegistry(ExampleState, store, "shopping", 60)
    next_turn = await recovered.claim("session")
    assert next_turn.session.state.seen == ["SKU-1"]
    await recovered.finish(next_turn)


async def test_completion_helper_preserves_result_and_error():
    assert await complete_before_cancelling(asyncio.sleep(0, result=42)) == 42

    async def fail():
        raise ValueError("worker failed")

    with pytest.raises(ValueError, match="worker failed"):
        await complete_before_cancelling(fail())


async def test_completion_drains_work_inside_cancelled_anyio_scope():
    finished = asyncio.Event()

    async def work():
        await asyncio.sleep(0.01)
        finished.set()

    with anyio.CancelScope() as scope:
        scope.cancel()
        await complete_before_cancelling(work())
    assert finished.is_set()


async def test_lost_heartbeat_stops_agent_and_releases_lease(engine_db):
    store = EngineStore(engine_db("sessions.db"))
    store.bind("session", "customer", "customer")
    registry = SessionRegistry(ExampleState, store, "shopping", 60)
    claimed = await registry.claim("session")
    claimed.heartbeat.cancel()
    await asyncio.gather(claimed.heartbeat, return_exceptions=True)
    started = asyncio.Event()
    closed = asyncio.Event()
    tool_called = False

    async def lose_lease():
        await started.wait()
        raise RuntimeError("chat turn lease was lost")

    async def agent():
        nonlocal tool_called
        try:
            yield "first event"
            started.set()
            await asyncio.Event().wait()
            tool_called = True
            yield "unsafe tool result"
        finally:
            closed.set()

    claimed.heartbeat = asyncio.create_task(lose_lease())
    received = []
    with pytest.raises(RuntimeError, match="lease was lost"):
        async with asyncio.timeout(5):
            async for event in registry.stream(claimed, agent()):
                received.append(event)
    assert received == ["first event"]
    assert closed.is_set()
    assert not tool_called
    with pytest.raises(RuntimeError, match="lease was lost"):
        await registry.finish(claimed)
    recovered = await registry.claim("session")
    await registry.finish(recovered)
