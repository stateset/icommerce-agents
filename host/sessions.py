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
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

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
    ) -> None:
        self._state_factory = state_factory
        self._store = store
        self._role = role
        self._lease_seconds = lease_seconds
        self._sessions: dict[str, ChatSession[StateT]] = {}

    def start(self, session_id: str) -> ChatSession[StateT]:
        session = ChatSession(session_id=session_id, state=self._state_factory())
        self._sessions[session_id] = session
        self._store.initialize_chat_session(
            session_id, self._role, session.state.model_dump_json(), "[]"
        )
        return session

    def get(self, session_id: str) -> ChatSession[StateT] | None:
        return self._sessions.get(session_id)

    def require(self, session_id: str) -> ChatSession[StateT]:
        try:
            return self._sessions[session_id]
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
        claimed = await asyncio.to_thread(
            self._store.claim_chat_turn,
            session_id,
            self._role,
            self._lease_seconds,
        )
        if claimed is None:
            if self._store.db_path == ":memory:" and current.turn_lock.locked():
                current.turn_lock.release()
            raise ChatTurnBusy(session_id)
        owner, snapshot = claimed
        if snapshot:
            try:
                current = ChatSession(
                    session_id=session_id,
                    state=self._state_factory.model_validate_json(snapshot["state_json"]),
                    messages=json.loads(snapshot["messages_json"]),
                )
            except Exception:
                await asyncio.to_thread(
                    self._store.release_chat_turn, session_id, self._role, owner
                )
                raise
            self._sessions[session_id] = current
        result = ClaimedChat(session=current, owner=owner)
        if self._store.db_path != ":memory:":
            result.heartbeat = asyncio.create_task(self._keepalive(result))
        return result

    async def _keepalive(self, claimed: ClaimedChat[StateT]) -> None:
        interval = max(10, self._lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                self._store.renew_chat_turn,
                claimed.session.session_id,
                self._role,
                claimed.owner,
                self._lease_seconds,
            )

    async def finish(self, claimed: ClaimedChat[StateT]) -> None:
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
            if self._store.db_path == ":memory:" and session.turn_lock.locked():
                session.turn_lock.release()

    def discard(self, session_id: str) -> None:
        """Forget chat transcript and provenance state when its binding is revoked."""
        self._sessions.pop(session_id, None)
