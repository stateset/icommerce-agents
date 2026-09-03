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

    The connection :meth:`_pin_connection` holds is per *process*, and what it holds is
    a share of the WAL index -- see that method for the mechanism and for why holding a
    file descriptor alone is not enough. Two host processes on one store file
    (``scripts/run_demo.py`` alongside a separately launched MCP server) are measured in
    ``tests/test_store_multiprocess.py``, and **a second process is covered precisely
    because it pins too.**

    The staleness is in-process in origin, which is the opposite of the obvious guess.
    POSIX advisory locks are held per process, so the lock a *second* process takes on
    the WAL index really does block this process from tearing it down; two SQLite
    libraries inside *one* process do not block each other at all. That is why a writer
    process that opens, writes and exits leaves a reader's handle correct, while the same
    open-and-close inside the reader's own process destroys it. An *unpinned* process
    that makes a transient ``sqlite3`` connection of its own -- one opened and closed
    around a single statement, which is exactly what :meth:`write_sql` does on every
    apply -- loses its ``Commerce`` handle's view of every later ``write_sql``,
    permanently, while disk stays correct. What matters is the open-and-close, not
    whether the statement is a read or a write: :meth:`readonly_sql` is *not* an instance
    of it, because it caches one connection per thread and holds it for that thread's
    life.

    So the pin is not redundant across processes and is not merely a single-process
    concern: it is the only thing standing between a deployment and silently stale
    prices. Each process that opens a store gets its own, which is why the per-process
    scope is the right scope rather than a gap.

    One caution for anyone reproducing this: an incidental extra connection anywhere in
    the harness masks the whole effect, which is how it stayed hidden here twice. A
    reader that performs an engine binding write before its read, or a diagnostic
    connection left open beside the store, reads correct values with the pin off and
    proves nothing. A third way to be misled is to reproduce it on one machine only: the
    symptom depends on the SQLite version behind Python's ``sqlite3`` (see
    :meth:`_pin_connection`), so a green run proves nothing about another host.

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
        """One read-only connection that joins the WAL index and then holds a share of it.

        This repo has two independent SQLite libraries open on one file: the engine's
        own (bundled in the Rust extension) and Python's ``sqlite3``, which
        :meth:`write_sql` and :meth:`readonly_sql` use for the reads and writes the
        binding does not expose. On a WAL database they coordinate through the
        shared-memory WAL index, the ``-shm`` file beside the ``-wal``. When a connection
        closes and SQLite believes it is the last user of that index, it checkpoints and
        **unlinks** ``-wal`` and ``-shm``. A handle that still has the unlinked index
        mapped keeps reading it: it is now a private copy of a file no one else will ever
        write to, so every WAL frame appended afterwards -- through the ``-wal`` the next
        connection creates -- is invisible to it, for the rest of its life, while the row
        on disk is correct.

        "Believes it is the last user" is where the two libraries part company. That
        belief rests on a POSIX advisory lock, and POSIX advisory locks are held per
        *process*: a lock taken by the engine's SQLite does not block Python's
        ``sqlite3`` in the same process from taking it exclusively, so
        :meth:`write_sql`'s connection unlinks the index out from under a live
        ``Commerce`` handle every time it closes. It is only within one library that
        SQLite tracks its own open connections and refuses. Hence the shape of this
        method: the pin must be a connection of *Python's* library, and it must actually
        be a user of the WAL index, not merely a descriptor on the file.

        That distinction is the whole fix. ``SELECT 1`` touches no table, so it opens no
        read transaction and never maps ``-shm``; a pin that only ran ``SELECT 1`` was
        counted by nothing and prevented nothing. Reading ``sqlite_schema`` opens a real
        read transaction, which maps the index and leaves this connection registered as a
        user of it for as long as the connection is open -- while releasing the read
        transaction itself, so checkpointing is not blocked and the WAL does not grow
        without bound.

        The symptoms of getting this wrong are not subtle, and all three were observed
        here: the engine's ``Commerce`` handle serving a pre-write price for the rest of
        its life while the row on disk is correct (so the *second* applied price update
        of a process silently never reaches the storefront -- the host holds one
        ``Commerce``, and ``catalog.list_variants`` resolves through
        ``get_variant_by_sku``); a read-only connection returning a row from an entirely
        different table; and ``PRAGMA wal_checkpoint(TRUNCATE)`` leaving the engine
        handle raising ``disk I/O error``.

        Whether the ``SELECT 1`` pin *appeared* to work depended on the SQLite version
        behind Python's ``sqlite3``, which is why this was believed settled twice.
        Measured on one machine, against the same engine build (``stateset-embedded``
        1.28.5 bundles SQLite 3.46.0 in both its sdist and its ``manylinux`` wheel), four
        successive ``write_sql`` price writes with a transient reader between them:
        stale from the second write on with Python's SQLite at 3.45.1, 3.46.0 and 3.47.1;
        correct with 3.50.4. The repo's own machine ships 3.50.4 and CI ships an older
        one, so the bug shipped and only CI ever saw it. With the ``sqlite_schema`` read
        below, all four versions track every write.

        The connection is never read from again: its only job is to hold that share. An
        in-memory store has no file to pin and no ``readonly_sql`` either, so it gets
        ``None``.
        """
        if self.db_path == ":memory:":
            return None
        connection = sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
        )
        # A real table read, run to completion: it maps the WAL index (which `SELECT 1`
        # does not) and then ends its read transaction (which `fetchone` on an unfinished
        # statement would not), leaving this connection a registered user of the index
        # without holding a read mark against the checkpointer.
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchall()
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
