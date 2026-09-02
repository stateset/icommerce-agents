import asyncio

import pytest

from engine_backend.store import EngineStore


@pytest.fixture
def store():
    return EngineStore(":memory:")


async def test_call_runs_on_a_worker_thread(store):
    count = await store.call(lambda c: c.products.count())
    assert count == 0


async def test_writes_for_one_session_are_serialized(store):
    order = []

    async def slow(tag):
        def body(_c):
            order.append(f"start-{tag}")
            order.append(f"end-{tag}")
            return tag

        return await store.write("s1", body)

    await asyncio.gather(slow("a"), slow("b"))
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
