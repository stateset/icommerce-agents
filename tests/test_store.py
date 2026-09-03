import asyncio
import threading
import time

import pytest

from engine_backend.store import EngineStore


@pytest.fixture
def store():
    return EngineStore(":memory:")


async def test_call_runs_on_a_worker_thread(store):
    count = await store.call(lambda c: c.products.count())
    assert count == 0


async def test_writes_for_one_session_are_serialized(store):
    """Both bodies run on worker threads and each holds a real window between its
    `start-` and `end-` marks, so the two would genuinely overlap if `EngineStore.write`
    did not serialize them. Remove the per-session lock and this fails: `order` comes
    back interleaved (`start-a, start-b, end-a, end-b`) and `peak` reaches 2.
    """
    order: list[str] = []
    inside = 0
    peak = 0
    counter_lock = threading.Lock()

    async def slow(tag):
        def body(_c):
            nonlocal inside, peak
            with counter_lock:
                inside += 1
                peak = max(peak, inside)
            order.append(f"start-{tag}")
            time.sleep(0.15)  # a real window for the other write to slip into
            order.append(f"end-{tag}")
            with counter_lock:
                inside -= 1
            return tag

        return await store.write("s1", body)

    await asyncio.gather(slow("a"), slow("b"))

    assert peak == 1, f"two writes for one session ran concurrently: {order}"
    assert order in (
        ["start-a", "end-a", "start-b", "end-b"],
        ["start-b", "end-b", "start-a", "end-a"],
    )


def test_binding_is_server_held(store):
    binding = store.bind("sess-1", "cust-9", "customer")
    assert binding.subject_id == "cust-9"
    assert binding.kind == "customer"
    assert store.binding("sess-1").subject_id == "cust-9"
    with pytest.raises(KeyError):
        store.binding("sess-unknown")


def test_readonly_sql_refuses_memory(store):
    with pytest.raises(RuntimeError):
        store.readonly_sql()
