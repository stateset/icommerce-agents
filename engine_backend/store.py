"""One engine handle per store file, plus the server-held session→principal binding."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar
from uuid import uuid4
from weakref import WeakValueDictionary

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, field_validator
from stateset_embedded import Commerce

from engine_backend.approvals import ApprovalLedger
from engine_backend.async_utils import complete_before_cancelling
from engine_backend.migrations import CONTROL_SCHEMA_VERSION as CONTROL_SCHEMA_VERSION
from engine_backend.migrations import upgrade_control_schema
from engine_backend.turn_locks import TurnLocks

T = TypeVar("T")


class MerchantOperationBusy(ValueError):
    """A live worker still owns an apply or reconciliation operation."""


class PrincipalBinding(BaseModel):
    """Who a session acts for. Set by the host at session start; never a tool argument."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    subject_id: str
    kind: Literal["customer", "operator"]
    store_id: str
    authenticated_subject: str | None = None
    expires_at: AwareDatetime | None = None

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


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

    Three operational facts about a second process matter:

    - Direct-SQL writes are serialized against each other by an ``asyncio`` lock, which
      reaches only as far as one process. Two processes writing at once are ordered by
      SQLite's own file lock and wait on the ``busy_timeout`` :meth:`write_sql` sets, not
      by that lock.
    - Approval, its single-use apply claim, and target leases are durable SQLite state.
      They survive restarts, exactly one process can claim a staged id, and different
      changes cannot mutate the same target concurrently. A crashed or ambiguous apply
      deliberately retains both its visible state and its leases for reconciliation.
    - Principal and session→cart bindings are durable adapter tables. File-backed
      principal reads always consult durable state so revocation is immediately visible
      across workers.
      Role-scoped chat transcripts and provenance state are durable too; an expiring
      database lease admits only one turn across all workers.
    """

    def __init__(self, db_path: str, store_id: str = "store:acme") -> None:
        if db_path == ":memory:":
            raise ValueError(
                "EngineStore needs a file-backed database: the control plane, the WAL pin, "
                "and cross-worker leases all live in that file"
            )
        self.db_path = db_path
        self.store_id = store_id
        # Owners and queued callers retain strong references; idle keys do not
        # retain one lock forever for every session this worker has ever seen.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._merchant_operations: set[str] = set()
        self._merchant_operations_lock = threading.Lock()
        self._sql = threading.local()
        self._turn_locks = TurnLocks(db_path)
        # Create adapter-owned tables before opening the embedded engine. Opening and
        # closing Python's SQLite afterwards can invalidate the engine binding's WAL
        # view on some SQLite builds (the pin below exists for the same reason).
        self._ensure_control_schema()
        self.approvals = ApprovalLedger(self)
        self.commerce = Commerce(db_path)
        self._pin: sqlite3.Connection = self._pin_connection()

    def _ensure_control_schema(self) -> None:
        """Create the adapter-owned durable control plane beside engine tables.

        This happens before the WAL pin is opened. Later control operations use
        transient connections while the pin keeps Python's SQLite from unlinking the
        WAL index underneath the engine's embedded SQLite handle. The schema itself
        lives in :mod:`engine_backend.migrations`.
        """
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            upgrade_control_schema(connection, self._now())
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _control_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @contextmanager
    def merchant_operation(self, change_id: str):
        """Exclude apply, resolution, and stale recovery for one proposal.

        Durable state still arbitrates approvals and target ownership. This guard
        additionally prevents a paused live operation being declared abandoned.
        Callers retain it until engine work and final bookkeeping have drained.
        """
        owner = uuid4().hex
        with self._merchant_operations_lock:
            if change_id in self._merchant_operations:
                raise MerchantOperationBusy(
                    "merchant operation is still active; retry after it finishes"
                )
            self._merchant_operations.add(change_id)
        try:
            if not self._turn_locks.acquire(change_id, "merchant-operation", owner):
                raise MerchantOperationBusy(
                    "merchant operation is still active; retry after it finishes"
                )
            try:
                yield
            finally:
                self._turn_locks.release(owner)
        finally:
            with self._merchant_operations_lock:
                self._merchant_operations.remove(change_id)

    async def call(self, fn: Callable[[Commerce], T]) -> T:
        return await complete_before_cancelling(asyncio.to_thread(fn, self.commerce))

    async def write(self, session_key: str, fn: Callable[[Commerce], T]) -> T:
        async with self.serialized(session_key):
            return await complete_before_cancelling(asyncio.to_thread(fn, self.commerce))

    @asynccontextmanager
    async def serialized(self, operation_key: str):
        """Serialize a compound in-process operation under ``operation_key``.

        ``write`` protects one binding call, but some correctness boundaries span
        multiple reads and writes. Those callers take a purpose-specific outer lock
        and retain their normal, different-key write locks inside it.
        """
        lock = self._locks.setdefault(operation_key, asyncio.Lock())
        async with lock:
            yield

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
            await complete_before_cancelling(asyncio.to_thread(body))

    def _pin_connection(self) -> sqlite3.Connection:
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

        The connection is never read from again: its only job is to hold that share.
        """
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
        connection: sqlite3.Connection | None = getattr(self._sql, "connection", None)
        if connection is None:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self._sql.connection = connection
        return connection

    def bind(
        self,
        session_id: str,
        subject_id: str,
        kind: Literal["customer", "operator"],
        *,
        authenticated_subject: str | None = None,
        expires_at: datetime | None = None,
    ) -> PrincipalBinding:
        binding = PrincipalBinding(
            session_id=session_id,
            subject_id=subject_id,
            kind=kind,
            store_id=self.store_id,
            authenticated_subject=authenticated_subject,
            expires_at=expires_at,
        )
        connection = self._control_connection()
        try:
            cursor = connection.execute(
                """
                INSERT INTO icommerce_agent_sessions (
                    session_id, subject_id, kind, store_id,
                    authenticated_subject, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    expires_at = excluded.expires_at
                WHERE subject_id = excluded.subject_id
                  AND kind = excluded.kind
                  AND store_id = excluded.store_id
                  AND authenticated_subject IS excluded.authenticated_subject
                """,
                (
                    session_id,
                    subject_id,
                    kind,
                    self.store_id,
                    authenticated_subject,
                    binding.expires_at.isoformat(timespec="microseconds")
                    if binding.expires_at is not None
                    else None,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("session identity cannot be rebound; start a new session")
            connection.commit()
        finally:
            connection.close()
        return binding

    def binding(self, session_id: str) -> PrincipalBinding:
        connection = self._control_connection()
        try:
            row = connection.execute(
                "SELECT * FROM icommerce_agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            try:
                binding = PrincipalBinding.model_validate(dict(row))
            except ValidationError as error:
                # Corrupt/legacy naive expiries have no unambiguous instant.
                # Deny access rather than guess a timezone or produce an HTTP 500.
                raise KeyError(session_id) from error
            if binding.expires_at is not None and binding.expires_at <= datetime.now(UTC):
                # A renewal can race this read. Remove only the expired
                # snapshot we observed, never its newly renewed replacement.
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "DELETE FROM icommerce_agent_sessions WHERE session_id = ? AND expires_at = ?",
                    (session_id, row["expires_at"]),
                )
                connection.commit()
                raise KeyError(session_id)
            return binding
        finally:
            connection.close()

    def unbind(self, session_id: str) -> None:
        """Revoke a durable session binding; missing ids are already revoked."""
        connection = self._control_connection()
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "DELETE FROM icommerce_agent_sessions WHERE session_id = ?", (session_id,)
            )
            connection.commit()
        finally:
            connection.close()

    def cleanup_expired_sessions(self) -> int:
        """Delete expired identity/workflow state and its chat/cart children."""
        connection = self._control_connection()
        try:
            now = datetime.now(UTC)
            connection.execute("PRAGMA foreign_keys = ON")
            rows = connection.execute(
                "SELECT * FROM icommerce_agent_sessions "
                "WHERE expires_at IS NOT NULL AND julianday(expires_at) <= julianday(?)",
                (now.isoformat(),),
            ).fetchall()
            expired = []
            for row in rows:
                try:
                    binding = PrincipalBinding.model_validate(dict(row))
                except ValidationError:
                    # Preserve ambiguous records for operator repair. binding()
                    # rejects them; cleanup must not invent an expiry instant.
                    continue
                # SQLite's date conversion handles legacy offsets but rounds to
                # milliseconds. Confirm in Python before deleting a future row.
                if binding.expires_at is not None and binding.expires_at <= now:
                    expired.append((row["session_id"], row["expires_at"]))
            cursor = connection.executemany(
                "DELETE FROM icommerce_agent_sessions WHERE session_id = ? AND expires_at = ?",
                expired,
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()

    def session_cart_id(self, session_id: str) -> str | None:
        connection = self._control_connection()
        try:
            row = connection.execute(
                "SELECT cart_id FROM icommerce_agent_session_carts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return str(row["cart_id"]) if row is not None else None
        finally:
            connection.close()

    def claim_session_cart(self, session_id: str, cart_id: str) -> str:
        """Install one durable cart per session and return the winning cart id.

        Two workers may both create an empty candidate, but the unique session key means
        every subsequent read/write converges on one winner before either adds a line.
        """
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO icommerce_agent_session_carts "
                "(session_id, cart_id, created_at) VALUES (?, ?, ?)",
                (session_id, cart_id, self._now()),
            )
            row = connection.execute(
                "SELECT cart_id FROM icommerce_agent_session_carts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("session cart claim disappeared")
            return str(row["cart_id"])
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize_chat_session(
        self, session_id: str, role: str, state_json: str, messages_json: str
    ) -> None:
        connection = self._control_connection()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO icommerce_agent_chat_sessions "
                "(session_id, role, state_json, messages_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, state_json, messages_json, self._now()),
            )
            connection.commit()
        finally:
            connection.close()

    def claim_chat_turn(
        self, session_id: str, role: str, lease_seconds: int
    ) -> tuple[str, dict[str, Any]] | None:
        """Atomically own a chat turn and return the latest durable snapshot."""
        owner = uuid4().hex
        if not self._turn_locks.acquire(session_id, role, owner):
            return None
        try:
            claimed = self._claim_chat_turn(session_id, role, lease_seconds, owner)
            if claimed is None:
                self._turn_locks.release(owner)
            return claimed
        except BaseException:
            self._turn_locks.release(owner)
            raise

    def _claim_chat_turn(
        self, session_id: str, role: str, lease_seconds: int, owner: str
    ) -> tuple[str, dict[str, Any]] | None:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=lease_seconds)
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions "
                "SET lease_owner = ?, lease_expires_at = ? "
                "WHERE session_id = ? AND role = ? "
                "AND (lease_owner IS NULL OR lease_expires_at <= ?)",
                (owner, expires.isoformat(), session_id, role, now.isoformat()),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT state_json, messages_json, revision "
                "FROM icommerce_agent_chat_sessions WHERE session_id = ? AND role = ?",
                (session_id, role),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("claimed chat session disappeared")
            return owner, dict(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_chat_turn(self, session_id: str, role: str, owner: str, lease_seconds: int) -> None:
        connection = self._control_connection()
        try:
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions SET lease_expires_at = ? "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (
                    (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(),
                    session_id,
                    role,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("chat turn lease was lost")
            connection.commit()
        finally:
            connection.close()

    def finish_chat_turn(
        self,
        session_id: str,
        role: str,
        owner: str,
        state_json: str,
        messages_json: str,
    ) -> None:
        try:
            self._finish_chat_turn(session_id, role, owner, state_json, messages_json)
        finally:
            self._turn_locks.release(owner)

    def _finish_chat_turn(
        self, session_id: str, role: str, owner: str, state_json: str, messages_json: str
    ) -> None:
        connection = self._control_connection()
        try:
            cursor = connection.execute(
                "UPDATE icommerce_agent_chat_sessions SET state_json = ?, "
                "messages_json = ?, revision = revision + 1, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = ? "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (state_json, messages_json, self._now(), session_id, role, owner),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("chat turn lease was lost before persistence")
            connection.commit()
        finally:
            connection.close()

    def release_chat_turn(self, session_id: str, role: str, owner: str) -> None:
        try:
            self._release_chat_turn(session_id, role, owner)
        finally:
            self._turn_locks.release(owner)

    def _release_chat_turn(self, session_id: str, role: str, owner: str) -> None:
        connection = self._control_connection()
        try:
            connection.execute(
                "UPDATE icommerce_agent_chat_sessions "
                "SET lease_owner = NULL, lease_expires_at = NULL "
                "WHERE session_id = ? AND role = ? AND lease_owner = ?",
                (session_id, role, owner),
            )
            connection.commit()
        finally:
            connection.close()

    def consume_rate_limit(self, principal: str, limit: int, window_start: int) -> bool:
        """Consume one fixed-minute allowance without storing a principal identifier."""
        if limit <= 0:
            return True
        key_hash = hashlib.sha256(principal.encode()).hexdigest()
        connection = self._control_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO icommerce_agent_rate_limits "
                "(key_hash, window_start, request_count) VALUES (?, ?, 1) "
                "ON CONFLICT(key_hash, window_start) DO UPDATE "
                "SET request_count = request_count + 1",
                (key_hash, window_start),
            )
            count = connection.execute(
                "SELECT request_count FROM icommerce_agent_rate_limits "
                "WHERE key_hash = ? AND window_start = ?",
                (key_hash, window_start),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM icommerce_agent_rate_limits WHERE window_start < ?",
                (window_start - 3600,),
            )
            connection.commit()
            return count <= limit
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
