"""One engine handle per store file, plus the server-held session→principal binding."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from pydantic import BaseModel
from stateset_embedded import Commerce

T = TypeVar("T")


class PrincipalBinding(BaseModel):
    """Who a session acts for. Set by the host at session start; never a tool argument."""

    session_id: str
    subject_id: str
    kind: Literal["customer", "operator"]
    store_id: str


class EngineStore:
    """One engine handle, one pinned connection, and the session→principal bindings.

    The connection :meth:`_pin_connection` holds is per *process*, so two host processes
    on one store file -- ``scripts/run_demo.py`` alongside a separately launched MCP
    server -- were an open question rather than a covered case. They are now measured, in
    ``tests/test_store_multiprocess.py``, and the answer is that **a second process is
    covered precisely because it pins too.**

    The staleness is not cross-process in origin: one process's direct-SQL connection
    churn does not by itself poison another process's handle, and a writer process that
    opens, writes and exits leaves a reader's handle correct. But a second process's
    writes are fully subject to the hazard. The moment an *unpinned* reader process makes
    a transient ``sqlite3`` connection of its own -- one opened and closed around a
    single statement, which is exactly what :meth:`write_sql` does on every apply -- its
    ``Commerce`` handle stops seeing the other process's ``write_sql`` writes,
    permanently, while disk is correct. What matters is the open-and-close, not whether
    the statement is a read or a write: :meth:`readonly_sql` is *not* an instance of it,
    because it caches one connection per thread and holds it for that thread's life.
    Measured over four successive price writes applied by a separate process, with the
    reader's pin dropped and its transient read left in, the handle tracks the first
    write and then freezes on it for the rest of its life; with the pin held it tracks
    all four. Deterministic over three repeats.

    So the pin is not redundant across processes and is not merely a single-process
    concern: it is the only thing standing between a two-process deployment and silently
    stale prices. Each process that opens a store gets its own, which is why the
    per-process scope is the right scope rather than a gap.

    One caution for anyone reproducing this: an incidental extra connection anywhere in
    the harness masks the whole effect, which is how it stayed hidden here twice. A
    reader that performs an engine binding write before its read, or a diagnostic
    connection left open beside the store, reads correct values with the pin off and
    proves nothing.

    Three things about a second process are true and are *not* guarantees this class
    makes:

    - Direct-SQL writes are serialized against each other by an ``asyncio`` lock, which
      reaches only as far as one process. Two processes writing at once are ordered by
      SQLite's own file lock and wait on the ``busy_timeout`` :meth:`write_sql` sets, not
      by that lock.
    - ``EngineMerchant._approved`` is an in-memory set and the gate ``apply_change``
      checks. Staged changes are persisted in ``custom_objects`` and so are shared, but
      the host approval that authorises applying one is not: an approval granted in one
      process does not authorise an apply in another, and does not survive a restart.
      This is the per-process item with the most riding on it, and the one most likely to
      be assumed shared.
    - The rest of the in-memory state beside the store is per process by construction:
      ``self._bindings`` here and ``EngineStorefront._cart_ids``, so a session belongs to
      the process that opened it.
    """

    def __init__(self, db_path: str, store_id: str = "store:acme") -> None:
        self.db_path = db_path
        self.store_id = store_id
        self.commerce = Commerce(db_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._bindings: dict[str, PrincipalBinding] = {}
        self._sql = threading.local()
        self._pin = self._pin_connection()

    async def call(self, fn: Callable[[Commerce], T]) -> T:
        return await asyncio.to_thread(fn, self.commerce)

    async def write(self, session_key: str, fn: Callable[[Commerce], T]) -> T:
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(fn, self.commerce)

    async def write_sql(self, sql: str, params: tuple[Any, ...]) -> None:
        """The only direct-SQL write path in this repo, and the only safe one.

        Three fields have no mutator in the engine's Python binding at all
        (``product_variants.price``, ``products.status``, ``products.description`` -- no
        ``products.update``, no variant-price setter), so they are written with a single
        parameterized ``UPDATE`` on a connection this method opens itself. Direct-SQL
        writes are serialized against each other under one lock key; ``busy_timeout``
        makes contention with a concurrent binding write wait rather than raise.

        This lives on the store rather than in ``merchant.py`` because a direct-SQL write
        is only sound in combination with the connection pinned in :meth:`_pin_connection`
        -- see that method for what goes wrong without it -- and putting the two in one
        class is what makes it impossible to add a direct-SQL write that forgets.
        """

        def body() -> None:
            connection = sqlite3.connect(self.db_path, timeout=30)
            try:
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.execute(sql, params)
                connection.commit()
            finally:
                connection.close()

        lock = self._locks.setdefault("direct_sql", asyncio.Lock())
        async with lock:
            await asyncio.to_thread(body)

    def _pin_connection(self) -> sqlite3.Connection | None:
        """One read-only connection, opened with the store and never used or closed.

        This repo has two independent SQLite libraries open on one file: the engine's
        own (bundled in the Rust extension) and Python's ``sqlite3``, which
        :meth:`write_sql` and :meth:`readonly_sql` use for the reads and writes the
        binding does not expose. On a WAL database they coordinate through the
        shared-memory WAL index, and that coordination is only stable while the file
        stays continuously open: let the last connection close, and the next connection
        rebuilds the index from scratch while another library's handle is still caching
        the old one.

        The symptoms of getting this wrong are not subtle, and all three were observed
        here: the engine's ``Commerce`` handle serving a pre-write price for the rest of
        its life while the row on disk is correct (so the *second* applied price update
        of a process silently never reaches the storefront -- the host holds one
        ``Commerce``, and ``catalog.list_variants`` resolves through
        ``get_variant_by_sku``); a read-only connection returning a row from an entirely
        different table; and ``PRAGMA wal_checkpoint(TRUNCATE)`` leaving the engine
        handle raising ``disk I/O error``.

        Holding one connection open for the life of the store removes the whole class:
        the file is never down to zero connections, so the WAL index is never torn down
        and rebuilt underneath a live handle. Measured over 60 write-then-read cycles
        with a transient reader opening and closing on every cycle, the engine handle is
        correct every time with this connection held and stale on all but the first
        without it, in both ``wal`` and ``delete`` journal modes.

        It is deliberately never read from: its only job is to exist. An in-memory store
        has no file to pin and no ``readonly_sql`` either, so it gets ``None``.
        """
        if self.db_path == ":memory:":
            return None
        connection = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
        )
        connection.execute("SELECT 1").fetchone()
        return connection

    def readonly_sql(self) -> sqlite3.Connection:
        """A read-only connection for the reads the binding does not expose.

        Listed in docs/mapping.md; every use is a single parameterized SELECT.

        One connection per thread, kept for the life of that thread. Callers reach this
        from two different places -- a worker thread, inside ``store.call(...)``
        (``catalog.list_variants``), and directly on the event loop
        (``merchant.execute_analysis_query``) -- and a ``sqlite3.Connection`` and its
        cursors are not safe to use from two threads concurrently. Sharing one
        connection would need every *use* serialized, not just the lazy connect; giving
        each thread its own removes the sharing instead, which is why
        ``check_same_thread`` is left at its default here.
        """
        if self.db_path == ":memory:":
            raise RuntimeError("a read-only connection needs a file-backed store, not :memory:")
        connection: sqlite3.Connection | None = getattr(self._sql, "connection", None)
        if connection is None:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self._sql.connection = connection
        return connection

    def bind(self, session_id: str, subject_id: str, kind: str) -> PrincipalBinding:
        binding = PrincipalBinding(
            session_id=session_id, subject_id=subject_id, kind=kind, store_id=self.store_id
        )
        self._bindings[session_id] = binding
        return binding

    def binding(self, session_id: str) -> PrincipalBinding:
        return self._bindings[session_id]
