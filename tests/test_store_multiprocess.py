"""What two host processes on one store file do to each other.

`EngineStore._pin_connection` holds one connection per *process*, so a second host
process -- `scripts/run_demo.py` plus a separately launched MCP server, say -- was an
open question. The measured answer is that **a second process is covered precisely
because it pins too**, and these tests encode both halves of that.

The staleness is not cross-process in origin, and the reason is that POSIX advisory locks
are held per process: another process's lock on the WAL index is one this process must
respect, while the engine's SQLite and Python's, inside one process, do not see each
other's at all. So a writer process that opens, writes and exits leaves a reader
process's handle correct. But a second process's writes are fully
subject to it. `test_an_unpinned_reader_process_goes_stale_...` is the negative leg: with
the reader's pin dropped and a transient `sqlite3` read left in -- a connection opened
and closed around one statement, which is what `write_sql` does on every apply -- the
reader's handle tracks the first write by another process and then freezes on it
permanently. It is the open-and-close that does it, not the statement being a read;
`readonly_sql()` is deliberately *not* that shape, because it caches one connection per
thread for the thread's life. That leg exists because without
it these tests could only confirm shipped behavior, never support the claim that the pin
is what makes two processes safe.

Reproducing any of this is unforgiving in one specific way, and it is why the wrong
conclusion was reached here twice: **an incidental extra connection anywhere masks the
whole effect.** A reader that performs an engine binding write before its read, or a
harness that leaves a diagnostic connection open beside the store, reads correct values
with the pin off and proves nothing. So these tests open nothing except each store's own
connection and the transient read that is itself under test, and they seed in a
throwaway child process -- an `EngineStore` left alive in the pytest process would hold
the file open for the whole session and mask everything.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from uuid import uuid4

import pytest

SKU = "TENT-RIDGE-TAN"
PRICES = ("199.00", "189.00", "179.00", "169.00")

# Bounds one child join, not a whole test. Children here take about a second; this is
# generous enough to absorb a loaded CI box while still failing rather than hanging.
JOIN_TIMEOUT = 60

try:  # a platform or sandbox without spawnable processes cannot run any of this
    _CTX: mp.context.BaseContext | None = mp.get_context("spawn")
    _CTX.Queue().close()  # type: ignore[union-attr]
except (ValueError, OSError, ImportError, PermissionError):  # pragma: no cover
    _CTX = None

pytestmark = pytest.mark.skipif(_CTX is None, reason="this platform cannot spawn worker processes")


def _log(commerce, summary: str) -> None:
    commerce.activity_logs.record(
        subject_type="staged_change",
        subject_id=str(uuid4()),
        action="apply",
        summary=summary,
        actor_kind="user",
        actor="user:acme-operator",
        metadata="{}",
    )


def _disk_price(db_path: str) -> str:
    """A short-lived reader, left to be collected rather than closed by hand.

    This connection is not incidental to the tests below -- it is the variable the whole
    finding turns on. Opening it is what tips an *unpinned* process's engine handle into
    permanent staleness. What it stands in for is a transient `sqlite3` connection
    opened and closed around one statement: `EngineStore.write_sql`, which every applied
    price change goes through. It is deliberately *not* standing in for `readonly_sql()`,
    which caches one connection per thread and never closes it. `_reader_process` takes
    it as a flag for that reason; do not "tidy" it into an unconditional call, or the
    negative leg stops testing anything.
    """
    return (
        sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        .execute("SELECT price FROM product_variants WHERE sku = ?", (SKU,))
        .fetchone()[0]
    )


def _seed(db_path: str) -> None:
    from engine_backend.seed import seed_store
    from engine_backend.store import EngineStore

    seed_store(EngineStore(db_path).commerce)


def _write_price(store, price: str) -> None:
    async def run() -> None:
        await store.write_sql(
            "UPDATE product_variants SET price = ?, updated_at = datetime('now'), "
            "version = version + 1 WHERE sku = ?",
            (price, SKU),
        )

    asyncio.run(run())
    # the engine binding write an apply performs straight after its direct SQL
    _log(store.commerce, f"set price to {price}")


def _writer_process(db_path: str, go, done) -> None:
    from engine_backend.store import EngineStore

    store = EngineStore(db_path)
    for price in PRICES:
        go.get()
        _write_price(store, price)
        done.put(price)


def _one_shot_writer_process(db_path: str, price: str) -> None:
    """A whole host process: open a store, apply one change, exit."""
    from engine_backend.store import EngineStore

    _write_price(EngineStore(db_path), price)


def _approval_claim_process(
    db_path: str, change_id: str, ready, go, out, targets: list[str] | None = None
) -> None:
    from engine_backend.store import EngineStore

    store = EngineStore(db_path)
    ready.put(True)
    go.wait()
    claim = store.claim_approval(change_id, "user:acme-operator", f"digest:{change_id}", targets)
    out.put((change_id, claim.attempt_id, claim.refusal, claim.blocked_target))


def _reader_process(db_path, ready, tick, out, *, pin: bool, disk_read: bool, log: bool):
    """A long-lived host process reading through its own engine handle.

    `pin=False` closes the connection the store pinned in its constructor, which is how
    the negative leg runs without editing `engine_backend/store.py`.
    """
    from engine_backend.store import EngineStore

    store = EngineStore(db_path)
    if not pin:
        store._pin.close()
        store._pin = None
    # warm the handle before any write happens, so a stale view has something to be
    # stale against -- this is the handle the in-process bug froze
    store.commerce.products.get_variant_by_sku(SKU)
    ready.set()
    while True:
        price = tick.get()
        if price is None:
            return
        if log:
            # this process's own binding write, after another process's direct SQL
            _log(store.commerce, f"observed {price}")
        disk = _disk_price(db_path) if disk_read else None
        out.put((price, store.commerce.products.get_variant_by_sku(SKU).price_exact, disk))


def _seeded(tmp_path, name: str = "store.db") -> str:
    db_path = str(tmp_path / name)
    process = _CTX.Process(target=_seed, args=(db_path,))
    process.start()
    process.join(JOIN_TIMEOUT)
    assert process.exitcode == 0, f"seeding process failed: exitcode {process.exitcode}"
    return db_path


def _start_reader(db_path: str, **flags):
    ready, tick, out = _CTX.Event(), _CTX.Queue(), _CTX.Queue()
    reader = _CTX.Process(target=_reader_process, args=(db_path, ready, tick, out), kwargs=flags)
    reader.start()
    assert ready.wait(JOIN_TIMEOUT), "the reader process never opened its store"
    return reader, tick, out


def _churn(db_path: str, tick, out) -> list[tuple[str, str, str | None]]:
    """Apply each price from its own short-lived process, reading between rounds."""
    rows = []
    for price in PRICES:
        writer = _CTX.Process(target=_one_shot_writer_process, args=(db_path, price))
        writer.start()
        writer.join(JOIN_TIMEOUT)
        assert writer.exitcode == 0, f"writer process failed: {writer.exitcode}"
        tick.put(price)
        rows.append(out.get(timeout=JOIN_TIMEOUT))
    return rows


def _stop(reader, tick) -> None:
    tick.put(None)
    reader.join(JOIN_TIMEOUT)


def _assert_tracks(rows: list[tuple[str, str, str | None]]) -> None:
    assert [row[0] for row in rows] == list(PRICES)
    for written, engine, disk in rows:
        if disk is not None:
            assert disk == written, f"the row on disk is wrong after writing {written}"
        assert engine == written, (
            f"another process's engine handle is serving a stale price: wrote {written}, "
            f"it reads {engine} (disk holds {disk})"
        )


def test_a_direct_sql_write_is_visible_to_another_processs_engine_handle(tmp_path):
    """The reader process opens first and warms its handle, then the writer process
    applies four successive direct-SQL price changes with a binding write after each --
    the exact shape that goes stale within one process. Across processes, with both
    stores pinned, every change is visible.
    """
    db_path = _seeded(tmp_path)
    reader, tick, out = _start_reader(db_path, pin=True, disk_read=True, log=True)
    go, done = _CTX.Queue(), _CTX.Queue()
    writer = _CTX.Process(target=_writer_process, args=(db_path, go, done))
    writer.start()
    try:
        rows = []
        for price in PRICES:
            go.put(True)
            assert done.get(timeout=JOIN_TIMEOUT) == price
            tick.put(price)
            rows.append(out.get(timeout=JOIN_TIMEOUT))
    finally:
        writer.join(JOIN_TIMEOUT)
        _stop(reader, tick)
    assert writer.exitcode == 0 and reader.exitcode == 0
    _assert_tracks(rows)


def test_a_writer_process_that_exits_between_writes_leaves_no_stale_handle(tmp_path):
    """The sharper ordering: each change is applied by a fresh process that opens a
    store, writes, and exits, so between rounds the long-lived reader is the only thing
    holding the file. Its handle still tracks every write.
    """
    db_path = _seeded(tmp_path)
    reader, tick, out = _start_reader(db_path, pin=True, disk_read=True, log=False)
    try:
        rows = _churn(db_path, tick, out)
    finally:
        _stop(reader, tick)
    assert reader.exitcode == 0
    _assert_tracks(rows)


def test_one_durable_approval_can_be_claimed_by_only_one_process(tmp_path):
    """The approval transition is a SQLite compare-and-set, not an asyncio lock.
    Separate worker processes racing the same approved id produce exactly one owner."""
    from engine_backend.store import EngineStore

    db_path = _seeded(tmp_path, "approval.db")
    store = EngineStore(db_path)
    change_id = "chg-cross-process"
    store.record_approval(change_id, "user:acme-operator", f"digest:{change_id}")

    ready, go, out = _CTX.Queue(), _CTX.Event(), _CTX.Queue()
    workers = [
        _CTX.Process(
            target=_approval_claim_process,
            args=(db_path, change_id, ready, go, out),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=JOIN_TIMEOUT) is True
    go.set()
    results = [out.get(timeout=JOIN_TIMEOUT) for _ in workers]
    for worker in workers:
        worker.join(JOIN_TIMEOUT)
        assert worker.exitcode == 0

    attempts = [attempt_id for _, attempt_id, _, _ in results if attempt_id is not None]
    refusals = [refusal for _, _, refusal, _ in results if refusal is not None]
    assert len(attempts) == 1
    assert refusals == ["already_claimed"]
    record = store.approval_record(change_id)
    assert record is not None
    assert record["state"] == "applying"
    assert record["attempt_id"] == attempts[0]


def test_different_changes_cannot_claim_the_same_target_across_processes(tmp_path):
    from engine_backend.store import EngineStore

    db_path = _seeded(tmp_path, "target-lease.db")
    store = EngineStore(db_path)
    change_ids = ["chg-target-a", "chg-target-b"]
    for change_id in change_ids:
        store.record_approval(change_id, "user:acme-operator", f"digest:{change_id}")

    ready, go, out = _CTX.Queue(), _CTX.Event(), _CTX.Queue()
    workers = [
        _CTX.Process(
            target=_approval_claim_process,
            args=(db_path, change_id, ready, go, out, [SKU]),
        )
        for change_id in change_ids
    ]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=JOIN_TIMEOUT) is True
    go.set()
    results = [out.get(timeout=JOIN_TIMEOUT) for _ in workers]
    for worker in workers:
        worker.join(JOIN_TIMEOUT)
        assert worker.exitcode == 0

    winner = next(result for result in results if result[1] is not None)
    loser = next(result for result in results if result[2] is not None)
    assert loser[2:] == ("target_claimed", SKU)
    assert store.approval_record(winner[0])["state"] == "applying"
    assert store.approval_record(loser[0])["state"] == "approved"

    store.finish_approval_attempt(winner[0], winner[1], outcome="failed", error="test release")
    retry = store.claim_approval(loser[0], "user:acme-operator", f"digest:{loser[0]}", [SKU])
    assert retry.attempt_id is not None


def test_an_unpinned_reader_process_goes_stale_on_another_processs_writes(tmp_path):
    """The negative leg, and the reason the pin is not redundant across processes.

    Same churn as above, but the reader closes the connection its store pinned. With its
    transient disk read left in -- a connection opened and closed around one statement,
    the shape `write_sql` takes on every apply -- its handle tracks the first write by
    the other process and then freezes on it permanently, while disk stays correct.
    Drop the transient read and the staleness disappears: that is the masking this
    module's docstring warns about, asserted here so a harness that quietly loses the
    read cannot pass by accident.

    If this test ever fails, the engine's behaviour has changed and the claims in
    `EngineStore`'s docstring and `docs/mapping.md` need re-measuring, not deleting.
    """
    db_path = _seeded(tmp_path, "stale.db")
    reader, tick, out = _start_reader(db_path, pin=False, disk_read=True, log=False)
    try:
        rows = _churn(db_path, tick, out)
    finally:
        _stop(reader, tick)
    assert reader.exitcode == 0

    first, *rest = rows
    assert first[1] == PRICES[0], "the first write should still have been visible"
    for written, engine, disk in rest:
        assert disk == written, f"the row on disk is wrong after writing {written}"
        assert engine == PRICES[0], (
            "an unpinned reader process is expected to freeze on the first write it saw; "
            f"after {written} it reads {engine}, not {PRICES[0]}"
        )

    # the same run without the reader's transient read: no staleness, nothing to see
    db_path = _seeded(tmp_path, "masked.db")
    reader, tick, out = _start_reader(db_path, pin=False, disk_read=False, log=False)
    try:
        rows = _churn(db_path, tick, out)
    finally:
        _stop(reader, tick)
    assert reader.exitcode == 0
    _assert_tracks(rows)
