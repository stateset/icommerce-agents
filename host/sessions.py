"""Server-held chat state for one role: the transcript and provenance state a session's
turns build up, keyed by the same session id the engine store binds identity under.

Identity itself lives in ``EngineStore``'s bindings (``store.bind`` / ``store.binding``);
this registry holds only what upstream's turn loop needs back on every turn — the
message list it extends in place, and the session state (``ShoppingSessionState`` /
``MerchantSessionState``) it mutates. Nothing here is returned to a client.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from engine_backend.async_utils import complete_before_cancelling
from engine_backend.store import EngineStore

SESSION_HEADER = "X-Session-Id"


@dataclass
class ChatSession[StateT: BaseModel]:
    session_id: str
    state: StateT
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Streaming a turn mutates both transcript and provenance state. Keep that pair
    # ordered even when one client submits overlapping requests for the same session.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class ClaimedChat[StateT: BaseModel]:
    session: ChatSession[StateT]
    owner: str
    heartbeat: asyncio.Task[None] | None = field(default=None, repr=False)


class ChatTurnBusy(RuntimeError):
    """Another worker still owns this session's current turn."""


class SessionRegistry[StateT: BaseModel]:
    """Durable role-scoped transcript/provenance state with a cross-worker turn lease."""

    def __init__(
        self,
        state_factory: type[StateT],
        store: EngineStore,
        role: Literal["shopping", "merchant"],
        lease_seconds: int,
        max_cached_sessions: int = 128,
    ) -> None:
        if max_cached_sessions < 1:
            raise ValueError("max_cached_sessions must be positive")
        self._state_factory = state_factory
        self._store = store
        self._role = role
        self._lease_seconds = lease_seconds
        self._sessions: OrderedDict[str, ChatSession[StateT]] = OrderedDict()
        self._active: dict[str, str] = {}
        self._max_cached_sessions = max_cached_sessions

    def _cache(self, session: ChatSession[StateT]) -> None:
        self._sessions[session.session_id] = session
        self._sessions.move_to_end(session.session_id)
        self._trim_cache()

    def _trim_cache(self) -> None:
        # Memory stores have no durable snapshot to reload after eviction.
        if self._store.db_path == ":memory:":
            return
        for session_id in list(self._sessions):
            if len(self._sessions) <= self._max_cached_sessions:
                break
            if session_id not in self._active:
                self._sessions.pop(session_id)

    def start(self, session_id: str) -> ChatSession[StateT]:
        session = ChatSession(session_id=session_id, state=self._state_factory())
        self._store.initialize_chat_session(
            session_id, self._role, session.state.model_dump_json(), "[]"
        )
        self._cache(session)
        return session

    def get(self, session_id: str) -> ChatSession[StateT] | None:
        session = self._sessions.get(session_id)
        if session is not None:
            self._sessions.move_to_end(session_id)
        return session

    def require(self, session_id: str) -> ChatSession[StateT]:
        try:
            session = self._sessions[session_id]
            self._sessions.move_to_end(session_id)
            return session
        except KeyError:
            # A bound session (the engine store knows it) with no chat record yet: a
            # cart-only session that never started a chat turn. Give it one lazily
            # rather than 401ing a caller who only ever used the direct routes.
            return self.start(session_id)

    async def claim(self, session_id: str) -> ClaimedChat[StateT]:
        current = self.require(session_id)
        if self._store.db_path == ":memory:":
            if current.turn_lock.locked():
                raise ChatTurnBusy(session_id)
            await current.turn_lock.acquire()
        claim_task = asyncio.create_task(
            asyncio.to_thread(
                self._store.claim_chat_turn,
                session_id,
                self._role,
                self._lease_seconds,
            )
        )
        try:
            claimed = await complete_before_cancelling(claim_task)
            if claimed is None:
                raise ChatTurnBusy(session_id)
        except BaseException:
            # The thread may have acquired a lease after its caller disconnected.
            # It has finished now, so release that exact owner before propagating.
            try:
                if not claim_task.cancelled() and claim_task.exception() is None:
                    acquired = claim_task.result()
                    if acquired is not None:
                        await complete_before_cancelling(
                            asyncio.to_thread(
                                self._store.release_chat_turn, session_id, self._role, acquired[0]
                            )
                        )
            finally:
                if self._store.db_path == ":memory:" and current.turn_lock.locked():
                    current.turn_lock.release()
            raise
        owner, snapshot = claimed
        if snapshot:
            try:
                current = ChatSession(
                    session_id=session_id,
                    state=self._state_factory.model_validate_json(snapshot["state_json"]),
                    messages=json.loads(snapshot["messages_json"]),
                )
            except Exception:
                await complete_before_cancelling(
                    asyncio.to_thread(self._store.release_chat_turn, session_id, self._role, owner)
                )
                raise
        self._active[session_id] = owner
        self._cache(current)
        result = ClaimedChat(session=current, owner=owner)
        if self._store.db_path != ":memory:":
            result.heartbeat = asyncio.create_task(self._keepalive(result))
        return result

    async def _keepalive(self, claimed: ClaimedChat[StateT]) -> None:
        interval = max(0.1, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await complete_before_cancelling(
                asyncio.to_thread(
                    self._store.renew_chat_turn,
                    claimed.session.session_id,
                    self._role,
                    claimed.owner,
                    self._lease_seconds,
                )
            )

    async def stream[EventT](
        self, claimed: ClaimedChat[StateT], events: AsyncIterator[EventT]
    ) -> AsyncIterator[EventT]:
        """Stop the agent promptly if its durable turn lease cannot be renewed."""
        iterator = aiter(events)
        try:
            while True:
                if claimed.heartbeat is not None and claimed.heartbeat.done():
                    claimed.heartbeat.result()
                    raise RuntimeError("chat turn heartbeat stopped")
                pending = asyncio.ensure_future(anext(iterator))
                try:
                    if claimed.heartbeat is not None:
                        await asyncio.wait(
                            (pending, claimed.heartbeat), return_when=asyncio.FIRST_COMPLETED
                        )
                        if claimed.heartbeat.done():
                            claimed.heartbeat.result()
                            raise RuntimeError("chat turn heartbeat stopped")
                    try:
                        yield await pending
                    except StopAsyncIteration:
                        return
                finally:
                    if not pending.done():
                        pending.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await complete_before_cancelling(pending)
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await complete_before_cancelling(close())

    async def finish(self, claimed: ClaimedChat[StateT]) -> None:
        # Streaming disconnects must not interrupt persistence or lease release.
        await complete_before_cancelling(self._finish(claimed))

    async def _finish(self, claimed: ClaimedChat[StateT]) -> None:
        session = claimed.session
        try:
            if claimed.heartbeat is not None:
                claimed.heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await claimed.heartbeat
            state_json = session.state.model_dump_json()
            messages_json = json.dumps(
                to_jsonable_python(session.messages),
                sort_keys=True,
                separators=(",", ":"),
            )
            await asyncio.to_thread(
                self._store.finish_chat_turn,
                session.session_id,
                self._role,
                claimed.owner,
                state_json,
                messages_json,
            )
        except BaseException:
            await asyncio.to_thread(
                self._store.release_chat_turn,
                session.session_id,
                self._role,
                claimed.owner,
            )
            raise
        finally:
            if self._active.get(session.session_id) == claimed.owner:
                self._active.pop(session.session_id)
            self._trim_cache()
            if self._store.db_path == ":memory:" and session.turn_lock.locked():
                session.turn_lock.release()

    def discard(self, session_id: str) -> None:
        """Forget chat transcript and provenance state when its binding is revoked."""
        self._sessions.pop(session_id, None)
