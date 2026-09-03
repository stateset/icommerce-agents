import asyncio
import sqlite3
import threading
import time
from uuid import uuid4

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


async def test_a_direct_sql_write_is_visible_to_the_engine_handle(tmp_path):
    """`write_sql` is only sound together with the connection `EngineStore` pins for its
    own lifetime: two SQLite libraries are open on this file (the engine's, bundled in
    the Rust extension, and Python's), and they stay coherent only while the file never
    drops to zero connections.

    Disable the pin and this fails on the second iteration: `commerce` keeps serving the
    first price for the rest of its life while the row on disk is correct. That is the
    bug in production terms -- the host holds one `Commerce`, `catalog.list_variants`
    resolves through `get_variant_by_sku`, so the second applied price change of a
    process would never reach listings, cart lines or `subtotal_exact`.

    `readonly_sql()` is deliberately never called here. It happens to hold a connection
    open too, which is what masked this for so long; the point of the pin is that the
    guarantee no longer depends on some other read having happened first.
    """
    from engine_backend.seed import seed_store
    from engine_backend.store import EngineStore

    store = EngineStore(str(tmp_path / "store.db"))
    seed_store(store.commerce)

    def disk_price():
        # A short-lived reader -- a second process, a backup, a debugging query -- left to
        # be collected rather than closed by hand, which is the ordinary case. One of
        # these coming and going is enough to desynchronise the handles when nothing else
        # holds the file open.
        return (
            sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True)
            .execute("SELECT price FROM product_variants WHERE sku = 'TENT-RIDGE-TAN'")
            .fetchone()[0]
        )

    for price in ("199.00", "189.00", "179.00", "169.00"):
        await store.write_sql(
            "UPDATE product_variants SET price = ?, updated_at = datetime('now'), "
            "version = version + 1 WHERE sku = ?",
            (price, "TENT-RIDGE-TAN"),
        )
        # An engine binding write between the direct-SQL writes, exactly as an apply does
        # (`_write_sql` then `_log_apply`'s activity_logs.record).
        store.commerce.activity_logs.record(
            subject_type="staged_change",
            subject_id=str(uuid4()),
            action="apply",
            summary=f"set price to {price}",
            actor_kind="user",
            actor="user:acme-operator",
            metadata="{}",
        )
        assert disk_price() == price, "the row on disk is wrong"
        variant = store.commerce.products.get_variant_by_sku("TENT-RIDGE-TAN")
        assert variant.price_exact == price, "the engine handle is serving a stale price"


def test_the_store_pins_a_connection_for_its_own_lifetime(tmp_path):
    store = EngineStore(str(tmp_path / "store.db"))
    assert store._pin is not None
    assert EngineStore(":memory:")._pin is None
