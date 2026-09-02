"""One engine handle per store file, plus the server-held session→principal binding."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from typing import Literal, TypeVar

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
    def __init__(self, db_path: str, store_id: str = "store:acme") -> None:
        self.db_path = db_path
        self.store_id = store_id
        self.commerce = Commerce(db_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._bindings: dict[str, PrincipalBinding] = {}
        self._sql: sqlite3.Connection | None = None
        self._sql_lock = threading.Lock()

    async def call(self, fn: Callable[[Commerce], T]) -> T:
        return await asyncio.to_thread(fn, self.commerce)

    async def write(self, session_key: str, fn: Callable[[Commerce], T]) -> T:
        lock = self._locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(fn, self.commerce)

    def readonly_sql(self) -> sqlite3.Connection:
        """A read-only connection for the reads the binding does not expose.

        Listed in docs/mapping.md; every use is a single parameterized SELECT.

        Thread-safe: guarded by a lock because callers may reach this from a worker
        thread inside store.call(...), so two threads could otherwise race the
        lazy connect and open two connections.
        """
        if self.db_path == ":memory:":
            raise RuntimeError("a read-only connection needs a file-backed store, not :memory:")
        with self._sql_lock:
            if self._sql is None:
                self._sql = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
                )
                self._sql.row_factory = sqlite3.Row
            return self._sql

    def bind(self, session_id: str, subject_id: str, kind: str) -> PrincipalBinding:
        binding = PrincipalBinding(
            session_id=session_id, subject_id=subject_id, kind=kind, store_id=self.store_id
        )
        self._bindings[session_id] = binding
        return binding

    def binding(self, session_id: str) -> PrincipalBinding:
        return self._bindings[session_id]
