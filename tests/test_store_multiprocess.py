"""What two host processes on one store file do to each other.

`EngineStore._pin_connection` holds one connection per *process*, so a second host
process -- `scripts/run_demo.py` plus a separately launched MCP server, say -- was an
open question: does the WAL-index staleness that
`tests/test_store.py::test_a_direct_sql_write_is_visible_to_the_engine_handle` pins down
cross the process boundary? These tests record the measured answer: it does not. A
`Commerce` handle tracks direct-SQL writes made by another process in every ordering
tried, and the in-process failure needs the direct-SQL connection churn and the handle
that goes stale to be in the same process.

Both tests do their own seeding in a throwaway child process rather than in the pytest
process. That is not tidiness: an extra `EngineStore` alive in the parent would hold the
file open for the whole session and is exactly the kind of incidental long-lived handle
that masks this effect.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3
from uuid import uuid4

import pytest

SKU = "TENT-RIDGE-TAN"
PRICES = ("199.00", "189.00", "179.00", "169.00")
TIMEOUT = 180

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
    """A short-lived reader, left to be collected rather than closed by hand."""
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


def _reader_process(db_path: str, opened, go, done, out) -> None:
    from engine_backend.store import EngineStore

    store = EngineStore(db_path)
    # warm the handle before any write happens, so a stale view has something to be
    # stale against -- this is the handle the in-process bug froze
    store.commerce.products.get_variant_by_sku(SKU)
    opened.set()
    for _ in PRICES:
        go.put(True)
        price = done.get()
        # this process's own binding write, after another process's direct SQL
        _log(store.commerce, f"observed {price}")
        out.put(
            (
                price,
                store.commerce.products.get_variant_by_sku(SKU).price_exact,
                _disk_price(db_path),
            )
        )


def _long_reader_process(db_path: str, opened, tick, out) -> None:
    from engine_backend.store import EngineStore

    store = EngineStore(db_path)
    store.commerce.products.get_variant_by_sku(SKU)
    opened.set()
    while True:
        price = tick.get()
        if price is None:
            return
        out.put(
            (
                price,
                store.commerce.products.get_variant_by_sku(SKU).price_exact,
                _disk_price(db_path),
            )
        )


def _seeded(tmp_path) -> str:
    db_path = str(tmp_path / "store.db")
    process = _CTX.Process(target=_seed, args=(db_path,))
    process.start()
    process.join(TIMEOUT)
    assert process.exitcode == 0, f"seeding process failed: exitcode {process.exitcode}"
    return db_path


def _check(rows: list[tuple[str, str, str]]) -> None:
    assert [row[0] for row in rows] == list(PRICES)
    for written, engine, disk in rows:
        assert disk == written, f"the row on disk is wrong after writing {written}"
        assert engine == written, (
            f"another process's engine handle is serving a stale price: wrote {written}, "
            f"it reads {engine} (disk holds {disk})"
        )


def test_a_direct_sql_write_is_visible_to_another_processs_engine_handle(tmp_path):
    """The reader process opens first and warms its handle, then the writer process
    applies four successive direct-SQL price changes with a binding write after each --
    the exact shape that goes stale within one process. Across processes every change is
    visible. The reader performs a binding write of its own between rounds, because in
    the single-process case that interleaved binding write is what froze the handle.
    """
    db_path = _seeded(tmp_path)
    opened, go, done, out = _CTX.Event(), _CTX.Queue(), _CTX.Queue(), _CTX.Queue()
    reader = _CTX.Process(target=_reader_process, args=(db_path, opened, go, done, out))
    writer = _CTX.Process(target=_writer_process, args=(db_path, go, done))
    reader.start()
    assert opened.wait(TIMEOUT), "the reader process never opened its store"
    writer.start()
    try:
        rows = [out.get(timeout=TIMEOUT) for _ in PRICES]
    finally:
        writer.join(TIMEOUT)
        reader.join(TIMEOUT)
    assert writer.exitcode == 0 and reader.exitcode == 0
    _check(rows)


def test_a_writer_process_that_exits_between_writes_leaves_no_stale_handle(tmp_path):
    """The sharper ordering: each change is applied by a fresh process that opens a
    store, writes, and exits, so between rounds the long-lived reader is the only thing
    holding the file. Its handle still tracks every write.
    """
    db_path = _seeded(tmp_path)
    opened, tick, out = _CTX.Event(), _CTX.Queue(), _CTX.Queue()
    reader = _CTX.Process(target=_long_reader_process, args=(db_path, opened, tick, out))
    reader.start()
    assert opened.wait(TIMEOUT), "the reader process never opened its store"
    rows = []
    try:
        for price in PRICES:
            writer = _CTX.Process(target=_one_shot_writer_process, args=(db_path, price))
            writer.start()
            writer.join(TIMEOUT)
            assert writer.exitcode == 0, f"writer process failed: {writer.exitcode}"
            tick.put(price)
            rows.append(out.get(timeout=TIMEOUT))
    finally:
        tick.put(None)
        reader.join(TIMEOUT)
    assert reader.exitcode == 0
    _check(rows)
