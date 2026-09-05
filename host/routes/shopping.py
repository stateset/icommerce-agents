"""Shopping chat, cart, order history, and the demo-only direct checkout.

The model never completes an order. ``checkout`` (the agent tool) renders the cart and
charges nothing; only the trusted routes here and in ``stablecoin`` complete one, and
both go through the governed ``checkout.commit`` kernel command."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..context import HostContext, build_cart_address
from ..schemas import (
    CartAddRequest,
    ChatTurnRequest,
    CheckoutRequest,
)

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    storefront = ctx.storefront
    authenticator = ctx.authenticator
    shopping_agent = ctx.shopping_agent
    shopping_sessions = ctx.shopping_sessions
    _bound_shopping_context = ctx.bound_shopping_context
    _cart_payload = ctx.cart_payload
    _commit_cart = ctx.commit_cart

    @router.post("/shopping/chat")
    async def shopping_chat(
        request: ChatTurnRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        session = _bound_shopping_context(x_session_id)
        claimed = await ctx.claim_chat(shopping_sessions, session.session_id)
        chat = claimed.session
        return ctx.stream_chat_turn(
            "shopping",
            shopping_sessions,
            claimed,
            lambda: shopping_agent.stream_turn(chat.messages, session, chat.state),
            message=request.message,
            request_id=http_request.state.request_id,
        )

    @router.post("/shopping/cart/add")
    async def shopping_cart_add(
        request: CartAddRequest, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        session = _bound_shopping_context(x_session_id)
        cart = await storefront.add_to_cart(session, request.product_id, request.quantity)
        return await _cart_payload(session, cart)

    @router.get("/shopping/cart")
    async def shopping_cart_read(x_session_id: str | None = Header(default=None)) -> dict[str, Any]:
        """A read: renders this session's own cart as it stands, no write involved.

        A session that has never added anything has no cart yet -- ``get_cart`` (via
        ``_cart_id``) would create one on first call, which is a write on a GET. Return
        an empty cart payload directly instead, without touching the store."""
        session = _bound_shopping_context(x_session_id)
        if storefront.session_cart_id(session.session_id) is None:
            return {
                "items": [],
                "currency": "USD",
                "subtotal_exact": None,
                "grand_total_exact": None,
            }
        cart = await storefront.get_cart(session)
        return await _cart_payload(session, cart)

    @router.get("/shopping/orders")
    async def shopping_orders(x_session_id: str | None = Header(default=None)) -> dict[str, Any]:
        """This session's own orders, each carrying the engine's own exact total --
        never a figure recomputed from the ``float`` total on the order itself."""
        session = _bound_shopping_context(x_session_id)
        # `storefront.get_orders` already scans and filters `c.orders.list()` down to
        # `Order` (the role-neutral shape, no exact-decimal or order-number fields).
        # This second scan below recovers those two engine-only fields per order; it is
        # a second full `orders.list()` on top of that first scan, not a second query
        # by id, because the engine has no `orders.get_many`. Cheap at demo scale; a
        # real deployment with many orders per customer would want `get_orders` itself
        # to carry these fields instead of a second full-table scan here.
        orders = await storefront.get_orders(session)
        binding = store.binding(session.session_id)
        engine_orders = {
            order.id: order
            for order in await store.call(lambda c: c.orders.list())
            if order.customer_id == binding.subject_id
        }
        payload = []
        for order in orders:
            item = order.model_dump(mode="json")
            engine_order = engine_orders.get(order.order_id)
            item["total_exact"] = (
                engine_order.total_amount_exact if engine_order is not None else None
            )
            item["order_number"] = engine_order.order_number if engine_order is not None else None
            payload.append(item)
        return {"orders": payload}

    @router.post("/shopping/checkout")
    async def shopping_checkout(
        http_request: Request,
        request: CheckoutRequest | None = None,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """The direct, non-payment demo route that completes an order. Reached by a
        human click, never by the model, and governed by ``checkout.commit``.

        This route exists only in demo mode and uses a fictional placeholder unless an
        address is supplied. Authenticated deployments must use a configured payment
        rail, so this endpoint can never create an unpaid production order."""
        if authenticator.config.mode != "demo":
            raise HTTPException(
                status_code=404,
                detail="direct checkout is demo-only; use a configured payment rail",
            )
        session = _bound_shopping_context(x_session_id)
        binding = store.binding(session.session_id)
        # This session's own cart, not the customer's most recent one: every shopping
        # session binds to the same seeded customer here, so picking the customer's last
        # cart would let two concurrent sessions check out each other's.
        cart_id = storefront.session_cart_id(session.session_id)
        if cart_id is None:
            raise HTTPException(status_code=409, detail="no cart to check out")
        customer = await store.call(lambda c: c.customers.get(binding.subject_id))
        if customer is None:
            raise HTTPException(status_code=409, detail="session customer no longer exists")

        supplied_address = request.shipping_address if request is not None else None
        # The fictional address is deliberately demo-only. Production JWT mode fails
        # closed before this route rather than creating an unpaid order.
        address = (
            build_cart_address(customer.email, **supplied_address.model_dump())
            if supplied_address is not None
            else build_cart_address(
                customer.email,
                first_name=customer.first_name or "",
                last_name=customer.last_name or "",
                company=None,
                line1="1 Demo Placeholder Way (ACME Supply fictional address)",
                line2=None,
                city="Fictional",
                state="ZZ",
                postal_code="00000",
                country="US",
                phone=None,
            )
        )

        return await _commit_cart(
            session_id=session.session_id,
            cart_id=cart_id,
            address=address,
            correlation_id=http_request.state.request_id,
        )

    return router
