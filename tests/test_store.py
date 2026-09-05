import asyncio
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from engine_backend.store import EngineStore


@pytest.fixture
def store(tmp_path):
    """A fresh, unseeded file-backed store."""
    return EngineStore(str(tmp_path / "store.db"))


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


def test_expired_binding_is_rejected_and_removed(store):
    store.bind(
        "expired",
        "cust-9",
        "customer",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(KeyError):
        store.binding("expired")
    with pytest.raises(KeyError):
        store.binding("expired")


def test_file_backed_binding_survives_store_recreation(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    first = EngineStore(db_path)
    first.bind("durable-session", "customer-7", "customer")

    second = EngineStore(db_path)
    assert second.binding("durable-session").subject_id == "customer-7"
    second.unbind("durable-session")
    with pytest.raises(KeyError):
        first.binding("durable-session")


def test_legacy_approval_ledger_is_upgraded_without_losing_records(tmp_path):
    db_path = str(tmp_path / "store.db")
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE icommerce_agent_approvals (
            change_id TEXT PRIMARY KEY,
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('approved', 'applying', 'applied', 'failed')),
            attempt_id TEXT,
            claimed_at TEXT,
            finished_at TEXT,
            last_error TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO icommerce_agent_approvals VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)",
        ("chg-legacy", "operator:7", "2026-01-01T00:00:00+00:00", "approved"),
    )
    connection.commit()
    connection.close()

    store = EngineStore(db_path)
    record = store.approval_record("chg-legacy")
    assert record["approved_by"] == "operator:7"
    assert record["proposal_digest"] is None
    connection = sqlite3.connect(db_path)
    schema = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'icommerce_agent_approvals'"
    ).fetchone()[0]
    migrations = connection.execute(
        "SELECT version, name FROM icommerce_agent_schema_migrations ORDER BY version"
    ).fetchall()
    connection.close()
    assert "reconciliation_required" in schema
    assert "reconciling" in schema
    assert "resolved" in schema
    assert migrations == [
        (1, "baseline-v0.9-control-plane"),
        (2, "stablecoin-refund-ledger"),
    ]


def test_control_schema_refuses_a_newer_database(tmp_path):
    db_path = str(tmp_path / "future.db")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE icommerce_agent_schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO icommerce_agent_schema_migrations VALUES (999, 'future', 'now')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="control schema is newer"):
        EngineStore(db_path)


def test_control_schema_upgrades_version_one_with_refund_ledger(tmp_path):
    db_path = str(tmp_path / "version-one.db")
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE icommerce_agent_schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO icommerce_agent_schema_migrations VALUES "
        "(1, 'baseline-v0.9-control-plane', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    EngineStore(db_path)

    connection = sqlite3.connect(db_path)
    version = connection.execute(
        "SELECT MAX(version) FROM icommerce_agent_schema_migrations"
    ).fetchone()[0]
    refund_table = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'icommerce_stablecoin_refunds'"
    ).fetchone()
    connection.close()
    assert version == 2
    assert refund_table == ("icommerce_stablecoin_refunds",)


def test_reconciliation_has_a_single_owner_and_normal_failures_are_retryable(store):
    digest = "sha256:" + "a" * 64
    store.record_approval("chg-1", "operator:1", digest)
    claim = store.claim_approval("chg-1", "operator:1", digest, ["sku:1"])
    store.finish_approval_attempt(
        "chg-1", claim.attempt_id, outcome="reconciliation_required", error="ambiguous"
    )

    store.claim_reconciliation("chg-1", "operator:1", digest, "accepted_current_state")
    with pytest.raises(ValueError, match="does not require reconciliation"):
        store.claim_reconciliation("chg-1", "operator:2", digest, "confirmed_applied")

    store.abort_reconciliation(
        "chg-1", "operator:1", digest, "accepted_current_state", "metadata write failed"
    )
    assert store.approval_record("chg-1")["state"] == "reconciliation_required"
    store.claim_reconciliation("chg-1", "operator:2", digest, "confirmed_applied")
    assert store.approval_record("chg-1")["resolved_by"] == "operator:2"


async def test_a_direct_sql_write_is_visible_to_the_engine_handle(tmp_path):
    """`write_sql` is only sound together with the connection `EngineStore` pins for its
    own lifetime: two SQLite libraries are open on this file (the engine's, bundled in
    the Rust extension, and Python's), they coordinate through the WAL index, and they
    stay coherent only while nothing unlinks that index under a live handle.

    Weaken the pin and this fails on the second iteration: `commerce` keeps serving the
    first price for the rest of its life while the row on disk is correct. That is the
    bug in production terms -- the host holds one `Commerce`, `catalog.list_variants`
    resolves through `get_variant_by_sku`, so the second applied price change of a
    process would never reach listings, cart lines or `subtotal_exact`.

    "Weaken", not only "disable": a pin that opens a connection but reads no table is as
    good as absent, and that is exactly the bug this test caught in CI while passing on
    the machine it was written on. What decides whether it shows up is the SQLite version
    behind Python's `sqlite3`, which the failure message names for that reason.

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
        assert variant.price_exact == price, (
            "the engine handle is serving a stale price "
            f"(Python's sqlite3 is {sqlite3.sqlite_version}; the engine bundles its own "
            "SQLite, and the two coordinate through the WAL index -- see "
            "`EngineStore._pin_connection`)"
        )


def test_the_store_pins_a_connection_for_its_own_lifetime(tmp_path):
    store = EngineStore(str(tmp_path / "store.db"))
    assert store._pin is not None


def test_in_memory_stores_are_refused():
    """The control plane, WAL pin, and OS-held leases all need a file."""
    with pytest.raises(ValueError, match="file-backed"):
        EngineStore(":memory:")


@pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="the WAL index is checked through /proc/self/fd, which only Linux has",
)
async def test_the_pin_keeps_the_wal_index_from_being_unlinked(tmp_path):
    """The mechanism the price test observes from the outside, asserted directly.

    `write_sql`'s connection closing is what used to unlink `-wal` and `-shm` -- Python's
    SQLite and the engine's do not see each other's advisory locks inside one process, so
    nothing stopped it -- leaving the engine's handle mapped to an index no one would
    write to again. The visible half is a frozen price; the physical half is a live
    descriptor on a deleted file, which is what this asserts.

    A pin that opens a connection but never reads a table (`SELECT 1`) never maps the
    index and does not prevent the unlink. Whether that shows up as a stale price depends
    on the SQLite version behind Python's `sqlite3` -- it did not on 3.50.4 and did on
    3.45.1, 3.46.0 and 3.47.1 -- so this test is the environment-independent half of the
    guarantee and the price test above is the behavioural half. Keep both.
    """
    from engine_backend.seed import seed_store

    db_path = str(tmp_path / "store.db")
    store = EngineStore(db_path)
    seed_store(store.commerce)
    before = os.stat(db_path + "-shm").st_ino

    await store.write_sql(
        "UPDATE product_variants SET price = ? WHERE sku = ?", ("149.00", "TENT-RIDGE-TAN")
    )
    # a transient reader of the ordinary kind: opened, used once, closed -- the shape
    # `write_sql` itself takes, and the close is the moment that used to unlink the index
    transient = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    transient.execute("SELECT 1 FROM products").fetchall()
    transient.close()

    deleted = []
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
        except OSError:  # pragma: no cover - the descriptor closed under us
            continue
        if target.startswith(db_path) and target.endswith("(deleted)"):
            deleted.append(target)
    assert not deleted, f"the WAL index was unlinked under a live engine handle: {deleted}"
    assert os.stat(db_path + "-shm").st_ino == before, (
        "the WAL index was replaced; the engine handle is still mapped to the old one"
    )
