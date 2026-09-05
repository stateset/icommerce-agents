"""Session minting and termination for both roles.

Identity is never a tool argument and never a request body field: ``POST .../session``
mints an unguessable id, binds it to a principal server-side, and returns only the id."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request

from ..auth import Identity
from ..context import _OPERATOR_ID, _ROWAN_EMAIL, HostContext

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    authenticator = ctx.authenticator
    shopping_sessions = ctx.shopping_sessions
    merchant_sessions = ctx.merchant_sessions
    session_ttl_seconds = ctx.settings.session_ttl_seconds
    _bound_shopping_context = ctx.bound_shopping_context
    _bound_merchant_context = ctx.bound_merchant_context

    @router.post("/shopping/session")
    async def start_shopping_session(request: Request) -> dict[str, str]:
        await asyncio.to_thread(store.cleanup_expired_sessions)
        identity: Identity | None = request.state.identity
        if authenticator.config.mode == "jwt":
            if identity is None or not identity.permits(role="customer", scope="shopping:use"):
                raise HTTPException(status_code=403, detail="shopping access required")
            if not identity.email:
                raise HTTPException(status_code=403, detail="customer email claim required")
            wanted = identity.email.casefold()
            customer = await store.call(
                lambda c: next(
                    (
                        item
                        for item in c.customers.list()
                        if item.email and item.email.casefold() == wanted
                    ),
                    None,
                )
            )
            if customer is None:
                raise HTTPException(status_code=403, detail="customer is not provisioned")
        else:
            customer = await store.call(lambda c: c.customers.get_by_email(_ROWAN_EMAIL))
            if customer is None:
                raise HTTPException(status_code=503, detail="demo customer is not seeded")
        session_id = secrets.token_urlsafe(24)
        store.bind(
            session_id,
            customer.id,
            "customer",
            authenticated_subject=identity.subject if identity else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=session_ttl_seconds),
        )
        shopping_sessions.start(session_id)
        return {"session_id": session_id}

    @router.post("/merchant/session")
    async def start_merchant_session(request: Request) -> dict[str, str]:
        await asyncio.to_thread(store.cleanup_expired_sessions)
        identity: Identity | None = request.state.identity
        if authenticator.config.mode == "jwt":
            if identity is None or not identity.permits(role="merchant", scope="merchant:write"):
                raise HTTPException(status_code=403, detail="merchant access required")
            if identity.store_id != store.store_id:
                raise HTTPException(
                    status_code=403, detail="token is not authorized for this store"
                )
            operator = identity.subject
        else:
            operator = _OPERATOR_ID
        session_id = secrets.token_urlsafe(24)
        store.bind(
            session_id,
            operator,
            "operator",
            authenticated_subject=identity.subject if identity else None,
            expires_at=datetime.now(UTC) + timedelta(seconds=session_ttl_seconds),
        )
        merchant_sessions.start(session_id)
        return {"session_id": session_id}

    @router.post("/shopping/session/end")
    async def end_shopping_session(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        session = _bound_shopping_context(x_session_id)
        shopping_sessions.discard(session.session_id)
        store.unbind(session.session_id)
        return {"status": "ended"}

    @router.post("/merchant/session/end")
    async def end_merchant_session(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        session = _bound_merchant_context(x_session_id)
        merchant_sessions.discard(session.session_id)
        store.unbind(session.session_id)
        return {"status": "ended"}

    return router
