"""Server-held chat state for one role: the transcript and provenance state a session's
turns build up, keyed by the same session id the engine store binds identity under.

Identity itself lives in ``EngineStore``'s bindings (``store.bind`` / ``store.binding``);
this registry holds only what upstream's turn loop needs back on every turn — the
message list it extends in place, and the session state (``ShoppingSessionState`` /
``MerchantSessionState``) it mutates. Nothing here is returned to a client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

SESSION_HEADER = "X-Session-Id"


@dataclass
class ChatSession[StateT: BaseModel]:
    session_id: str
    state: StateT
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Streaming a turn mutates both transcript and provenance state. Keep that pair
    # ordered even when one client submits overlapping requests for the same session.
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class SessionRegistry[StateT: BaseModel]:
    """One role's chat sessions, in process memory. ``state_factory`` builds the empty
    per-role state a fresh session starts with (``ShoppingSessionState`` or
    ``MerchantSessionState``)."""

    def __init__(self, state_factory: type[StateT]) -> None:
        self._state_factory = state_factory
        self._sessions: dict[str, ChatSession[StateT]] = {}

    def start(self, session_id: str) -> ChatSession[StateT]:
        session = ChatSession(session_id=session_id, state=self._state_factory())
        self._sessions[session_id] = session
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
