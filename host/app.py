"""The FastAPI host: both commerce-agents roles behind HTTP, over one engine store.

Two rules this module exists to enforce:

- The model never completes an order. ``checkout`` (the agent tool) renders the cart and
  charges nothing; ``POST /shopping/checkout`` is the only route that completes one, it
  goes through the governed ``checkout.commit`` kernel command, and no agent tool reaches
  it — a human click is what does.
- Approval is the operator's, and it happens here. ``POST /merchant/changes/{id}/approve``
  is the only place ``EngineMerchant.approve`` is called, the operator comes from the
  session binding, never the request body, and an unknown change id is a 404 before that
  call is ever made.

Identity is never a tool argument and never a request body field: ``POST .../session``
mints an unguessable id, binds it to a principal server-side, and returns only the id.
Every other route reads it back from ``X-Session-Id``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from commerce_common.streaming import AgentEvent, to_sse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from merchant_agent.changes import ChangeNotApplicable
from merchant_agent.types import (
    ActorKind,
    ChangeStatus,
    MerchantSessionContext,
    MerchantSessionState,
    StagedChange,
)
from merchant_agent_runtime import MerchantAgent
from pydantic import BaseModel, Field
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent
from stateset_embedded import CartAddress

from engine_backend import SKILLS_DIR, refunds, staging
from engine_backend.agent_config import merchant_agent_config, shopping_agent_config
from engine_backend.custom_objects import list_payloads
from engine_backend.kernel import KernelClient, approval_evidence
from engine_backend.merchant import EngineMerchant
from engine_backend.reconciliation import assess as assess_reconciliation
from engine_backend.seed import seed_store
from engine_backend.staging import STAGED_TYPE, load_evidence
from engine_backend.store import EngineStore
from engine_backend.storefront import EngineStorefront

from .anthropic_client import build_anthropic_client
from .auth import AuthConfig, AuthenticationError, Authenticator, Identity
from .metrics import HostMetrics
from .response_policy import TurnResponsePolicy, replace_latest_assistant_text
from .sessions import SessionRegistry

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_ROWAN_EMAIL = "rowan@example.invalid"
_OPERATOR_ID = "user:acme-operator"
_REQUEST_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
logger = logging.getLogger(__name__)


def _valid_request_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and all(char in _REQUEST_ID_CHARS for char in value)


class ChatTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class CartAddRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=200)
    # This direct UI route bypasses the shopping executor, so carry its default
    # per-item quantity boundary at the HTTP edge too.
    quantity: int = Field(default=1, ge=1, le=24)


class ShippingAddressRequest(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    company: str | None = Field(default=None, max_length=200)
    line1: str = Field(min_length=1, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=120)
    postal_code: str = Field(min_length=1, max_length=32)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    phone: str | None = Field(default=None, max_length=40)


class CheckoutRequest(BaseModel):
    shipping_address: ShippingAddressRequest | None = None


class RefundPreviewRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class RefundApplyRequest(RefundPreviewRequest):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class ReconciliationRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolution: Literal["confirmed_applied", "accepted_current_state"]


class ApprovalRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReconciliationStartRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


# ``engine_backend.apply`` records one of two evidence shapes at apply time: a sealed
# kernel receipt id for a governed write, or an activity-log id for one it only logged.
# ``engine_backend.staging`` persists that as a structured ``Evidence`` list alongside
# the change, keyed by ``change_id`` -- never inferred from ``guardrail_notes`` prose, so
# a wording change to a note cannot make evidence disappear from the portal.
async def _with_change_evidence(store: EngineStore, event: AgentEvent) -> AgentEvent:
    if event.type != "change_update":
        return event
    change = dict(event.data.get("change") or {})
    # Two reasons to hand the event straight back. A change dict with no `change_id` is
    # nothing this can look up, and raising here would break the SSE stream mid-turn
    # rather than drop one field. And evidence only exists for an *applied* change --
    # `apply.apply_change` is what produces it -- so a staged or discarded change would
    # cost a database read per event to learn it has none.
    change_id = change.get("change_id")
    if change_id is None or change.get("status") != ChangeStatus.APPLIED.value:
        return event
    evidence = await load_evidence(store, change_id)
    change["evidence"] = [item.model_dump() for item in evidence]
    return AgentEvent(type=event.type, data={**event.data, "change": change})


def _session_id(x_session_id: str | None) -> str:
    if not x_session_id:
        raise HTTPException(status_code=401, detail="missing X-Session-Id")
    return x_session_id


def create_app(
    db_path: str,
    auth_config: AuthConfig | None = None,
    *,
    stale_apply_seconds: int | None = None,
) -> FastAPI:
    """Build one deployment: one engine store (seeded), both backends, one kernel
    client bound to the host-owned policy and principal files, and both agents."""
    store = EngineStore(db_path)
    seed_store(store.commerce)

    storefront = EngineStorefront(store)
    kernel = KernelClient(
        store, CONFIG_DIR / "kernel-policy.json", CONFIG_DIR / "kernel-principal.json"
    )
    merchant = EngineMerchant(store, kernel)
    authenticator = Authenticator(auth_config or AuthConfig.from_env())
    stale_apply_seconds = (
        int(os.getenv("ICOMMERCE_STALE_APPLY_SECONDS", "900"))
        if stale_apply_seconds is None
        else stale_apply_seconds
    )
    if stale_apply_seconds < 1:
        raise ValueError("stale apply recovery threshold must be at least one second")
    session_ttl_seconds = int(os.getenv("ICOMMERCE_SESSION_TTL_SECONDS", "28800"))
    if session_ttl_seconds < 60:
        raise ValueError("session TTL must be at least 60 seconds")

    anthropic_client = build_anthropic_client()
    shopping_agent = ShoppingAgent(
        backend=storefront,
        skills_dir=SKILLS_DIR("shopping"),
        config=shopping_agent_config(),
        client=anthropic_client,
    )
    merchant_agent = MerchantAgent(
        backend=merchant,
        skills_dir=SKILLS_DIR("merchant"),
        config=merchant_agent_config(),
        client=anthropic_client,
    )

    shopping_sessions: SessionRegistry[ShoppingSessionState] = SessionRegistry(ShoppingSessionState)
    merchant_sessions: SessionRegistry[MerchantSessionState] = SessionRegistry(MerchantSessionState)
    metrics = HostMetrics()
    metrics_token = os.getenv("ICOMMERCE_METRICS_TOKEN")
    if metrics_token is not None and len(metrics_token.encode()) < 32:
        raise ValueError("metrics token must be at least 32 bytes")

    app = FastAPI(title="StateSet iCommerce agents host")

    # `web/storefront` (:3000) and `web/portal` (:3100) call this host from their own
    # origin -- there is no reverse proxy or Next.js rewrite in front of either -- so a
    # browser refuses to expose any response to their JS without this. Production
    # origins are configured explicitly; there is deliberately no wildcard fallback.
    allowed_origins = [
        origin.strip()
        for origin in os.getenv(
            "ICOMMERCE_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3100"
        ).split(",")
        if origin.strip()
    ]

    @app.middleware("http")
    async def authenticate_commerce_request(request: Request, call_next):
        """Verify identity before any commerce route can reach an agent or engine.

        In JWT mode a session id is only a workflow handle, never a bearer credential:
        subsequent requests must present the same signed subject that created it.
        """
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith(("/shopping/", "/merchant/")):
            return await call_next(request)
        try:
            identity = await asyncio.to_thread(
                authenticator.authenticate, request.headers.get("Authorization")
            )
        except AuthenticationError as error:
            return JSONResponse(
                status_code=401,
                content={"detail": str(error)},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.identity = identity
        if authenticator.config.mode == "demo" or path in (
            "/shopping/session",
            "/merchant/session",
        ):
            return await call_next(request)
        session_id = request.headers.get("X-Session-Id")
        if not session_id:
            return JSONResponse(status_code=401, content={"detail": "missing X-Session-Id"})
        try:
            binding = store.binding(session_id)
        except KeyError:
            return JSONResponse(status_code=401, content={"detail": "unknown session"})
        expected_kind = "customer" if path.startswith("/shopping/") else "operator"
        if binding.kind != expected_kind:
            return JSONResponse(status_code=403, content={"detail": "session role mismatch"})
        if identity is None or binding.authenticated_subject != identity.subject:
            return JSONResponse(status_code=403, content={"detail": "session subject mismatch"})
        if expected_kind == "customer" and not identity.permits(
            role="customer", scope="shopping:use"
        ):
            return JSONResponse(status_code=403, content={"detail": "shopping access required"})
        if expected_kind == "operator" and (
            not identity.permits(role="merchant", scope="merchant:write")
            or identity.store_id != store.store_id
        ):
            return JSONResponse(status_code=403, content={"detail": "merchant access required"})
        return await call_next(request)

    # Added after the auth middleware so CORS wraps even an early 401/403 response.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Session-Id"],
    )

    @app.middleware("http")
    async def correlate_and_secure(request: Request, call_next):
        """Attach a non-secret correlation id and safe API response defaults."""
        supplied = request.headers.get("X-Request-Id", "")
        request_id = supplied if _valid_request_id(supplied) else secrets.token_hex(16)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed method=%s path=%s request_id=%s",
                request.method,
                request.url.path,
                request_id,
            )
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        metrics.request(request.method, route_path, response.status_code)
        response.headers["X-Request-Id"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        logger.info(
            "request completed method=%s path=%s status=%d elapsed_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    # -- Health -----------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Prove the engine can answer a read, rather than only that Python is alive."""
        try:
            await store.call(lambda commerce: commerce.products.count())
        except Exception as error:
            raise HTTPException(status_code=503, detail="engine store is unavailable") from error
        return {"status": "ready"}

    @app.get("/metrics")
    async def prometheus_metrics(request: Request) -> PlainTextResponse:
        """Low-cardinality metrics, disabled until a dedicated token is configured."""
        if not metrics_token:
            raise HTTPException(status_code=404, detail="metrics are disabled")
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {metrics_token}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="metrics authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    # -- Bindings -----------------------------------------------------------------

    def _bound_shopping_context(x_session_id: str | None) -> ShoppingSessionContext:
        session_id = _session_id(x_session_id)
        try:
            binding = store.binding(session_id)
        except KeyError as error:
            raise HTTPException(status_code=401, detail="unknown session") from error
        if binding.kind != "customer":
            raise HTTPException(status_code=401, detail="not a shopping session")
        return ShoppingSessionContext(session_id=session_id, user_id=binding.subject_id)

    def _bound_merchant_context(x_session_id: str | None) -> MerchantSessionContext:
        session_id = _session_id(x_session_id)
        try:
            binding = store.binding(session_id)
        except KeyError as error:
            raise HTTPException(status_code=401, detail="unknown session") from error
        if binding.kind != "operator":
            raise HTTPException(status_code=401, detail="not a merchant session")
        return MerchantSessionContext(
            session_id=session_id, merchant_id=store.store_id, operator=binding.subject_id
        )

    # -- Sessions -----------------------------------------------------------------

    @app.post("/shopping/session")
    async def start_shopping_session(request: Request) -> dict[str, str]:
        identity: Identity | None = request.state.identity
        if authenticator.config.mode == "jwt":
            if identity is None or not identity.permits(role="customer", scope="shopping:use"):
                raise HTTPException(status_code=403, detail="shopping access required")
            if not identity.email:
                raise HTTPException(status_code=403, detail="customer email claim required")
            customer = await store.call(
                lambda c: next(
                    (
                        item
                        for item in c.customers.list()
                        if item.email and item.email.casefold() == identity.email.casefold()
                    ),
                    None,
                )
            )
            if customer is None:
                raise HTTPException(status_code=403, detail="customer is not provisioned")
        else:
            customer = await store.call(lambda c: c.customers.get_by_email(_ROWAN_EMAIL))
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

    @app.post("/merchant/session")
    async def start_merchant_session(request: Request) -> dict[str, str]:
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

    @app.post("/shopping/session/end")
    async def end_shopping_session(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        session = _bound_shopping_context(x_session_id)
        shopping_sessions.discard(session.session_id)
        store.unbind(session.session_id)
        return {"status": "ended"}

    @app.post("/merchant/session/end")
    async def end_merchant_session(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, str]:
        session = _bound_merchant_context(x_session_id)
        merchant_sessions.discard(session.session_id)
        store.unbind(session.session_id)
        return {"status": "ended"}

    # -- Shopping: chat, cart, checkout --------------------------------------------

    @app.post("/shopping/chat")
    async def shopping_chat(
        request: ChatTurnRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        session = _bound_shopping_context(x_session_id)
        chat = shopping_sessions.require(session.session_id)

        async def event_stream() -> AsyncIterator[str]:
            async with chat.turn_lock:
                chat.messages.append({"role": "user", "content": request.message})
                policy = TurnResponsePolicy("shopping", request.message)
                async for event in shopping_agent.stream_turn(chat.messages, session, chat.state):
                    for checked in policy.accept(event):
                        yield to_sse(checked)
                for checked in policy.flush():
                    yield to_sse(checked)
                if policy.rewritten:
                    replace_latest_assistant_text(chat.messages, policy.final_text)
                    metrics.policy_rewrite("shopping")
                    logger.warning(
                        "response policy rewrote role=shopping request_id=%s",
                        http_request.state.request_id,
                    )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    async def _cart_payload(session: ShoppingSessionContext, cart: Any) -> dict[str, Any]:
        payload = cart.model_dump(mode="json")
        # The engine's own exact decimal totals, not a figure recomputed from the
        # ``float`` prices above: the browser displays these as given, never multiplies.
        exact = await storefront.cart_exact_totals(session)
        for item in payload["items"]:
            item["total_exact"] = exact["line_totals_exact"].get(item["product_id"])
        payload["subtotal_exact"] = exact["subtotal_exact"]
        payload["grand_total_exact"] = exact["grand_total_exact"]
        return payload

    @app.post("/shopping/cart/add")
    async def shopping_cart_add(
        request: CartAddRequest, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        session = _bound_shopping_context(x_session_id)
        cart = await storefront.add_to_cart(session, request.product_id, request.quantity)
        return await _cart_payload(session, cart)

    @app.get("/shopping/cart")
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

    @app.get("/shopping/orders")
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

    @app.post("/shopping/checkout")
    async def shopping_checkout(
        http_request: Request,
        request: CheckoutRequest | None = None,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """The only route that completes an order. Reached by a human click, never by
        the model: no agent tool calls this. Governed by ``checkout.commit``.

        Authenticated deployments must supply a validated shipping address. Demo mode
        uses a fixed fictional placeholder so the keyless tour can satisfy the engine's
        checkout-readiness check without collecting personal data."""
        session = _bound_shopping_context(x_session_id)
        binding = store.binding(session.session_id)
        # This session's own cart, not the customer's most recent one: every shopping
        # session binds to the same seeded customer here, so picking the customer's last
        # cart would let two concurrent sessions check out each other's.
        cart_id = storefront.session_cart_id(session.session_id)
        if cart_id is None:
            raise HTTPException(status_code=409, detail="no cart to check out")
        customer = await store.call(lambda c: c.customers.get(binding.subject_id))

        supplied_address = request.shipping_address if request is not None else None
        if authenticator.config.mode == "jwt" and supplied_address is None:
            raise HTTPException(
                status_code=422,
                detail="shipping_address is required for authenticated checkout",
            )

        # The fictional address is deliberately demo-only. Production JWT mode fails
        # closed above rather than allowing an order to inherit data the shopper never
        # supplied.
        address = (
            CartAddress(email=customer.email, **supplied_address.model_dump())
            if supplied_address is not None
            else CartAddress(
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
                email=customer.email,
            )
        )

        def set_address(c: Any) -> None:
            c.carts.set_shipping_address(cart_id, address)

        await store.write(session.session_id, set_address)
        receipt = await kernel.execute(
            "checkout.commit",
            {"cart_id": cart_id},
            idempotency_key=f"checkout-{cart_id}",
            correlation_id=http_request.state.request_id,
        )
        metrics.kernel_command("checkout.commit", receipt.status or "unknown")
        if not receipt.ok:
            raise HTTPException(
                status_code=422,
                detail={"error_code": receipt.error_code, "error_message": receipt.error_message},
            )
        result = receipt.result or {}
        return {
            "order_number": result.get("order_number"),
            "receipt": receipt.model_dump(mode="json"),
        }

    # -- Merchant: chat, approval ---------------------------------------------------

    @app.post("/merchant/chat")
    async def merchant_chat(
        request: ChatTurnRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        session = _bound_merchant_context(x_session_id)
        chat = merchant_sessions.require(session.session_id)

        async def event_stream() -> AsyncIterator[str]:
            async with chat.turn_lock:
                chat.messages.append({"role": "user", "content": request.message})
                policy = TurnResponsePolicy("merchant", request.message)
                try:
                    async for event in merchant_agent.stream_turn(
                        chat.messages, session, chat.state
                    ):
                        enriched = await _with_change_evidence(store, event)
                        for checked in policy.accept(enriched):
                            yield to_sse(checked)
                    for checked in policy.flush():
                        yield to_sse(checked)
                    if policy.rewritten:
                        replace_latest_assistant_text(chat.messages, policy.final_text)
                        metrics.policy_rewrite("merchant")
                        logger.warning(
                            "response policy rewrote role=merchant request_id=%s",
                            http_request.state.request_id,
                        )
                finally:
                    # The upstream gate and the engine adapter deliberately enforce
                    # approval independently. The backend consumes its mark on an apply
                    # attempt; mirror that consumption into session state after every
                    # turn so a failed attempt cannot retain a stale upstream approval.
                    chat.state.approved_change_ids.intersection_update(merchant.approved_ids)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/merchant/changes/{change_id}/approve")
    async def merchant_approve_change(
        change_id: str,
        request: ApprovalRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """The operator's approval, and the only place it happens. The operator comes
        from the session binding, never from the request body."""
        session = _bound_merchant_context(x_session_id)
        chat = merchant_sessions.require(session.session_id)
        # Serialize approval against this session's streaming turns. Otherwise a turn's
        # final reconciliation could erase an approval issued while that turn was still
        # in flight.
        async with chat.turn_lock:
            record = await staging.load_record(store, change_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
            change = StagedChange.model_validate(record)
            if change.status is not ChangeStatus.STAGED:
                # An already-applied or discarded change has nothing left to approve;
                # accepting one would put a live change id into `approved_ids`.
                raise HTTPException(
                    status_code=409,
                    detail=f"change {change_id} is {change.status.value}, not staged",
                )
            digest = staging.proposal_digest(change, record.get("payload"))
            if digest != record.get("proposal_digest") or digest != request.proposal_digest:
                raise HTTPException(status_code=409, detail="proposal digest changed")
            # Claude Commerce's executor checks this session-owned mark before it calls
            # the backend. The backend checks a separate operator-bound mark again at
            # the mutation boundary; both are required for the HTTP path.
            try:
                merchant.approve(change_id, session.operator)
            except ChangeNotApplicable as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            chat.state.approved_change_ids.add(change_id)
        approval = store.approval_record(change_id)
        return {
            "change_id": change_id,
            "approved_by": session.operator,
            "proposal_digest": approval["proposal_digest"],
        }

    @app.get("/merchant/changes/{change_id}/reconciliation")
    async def merchant_reconciliation_read(
        change_id: str, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _bound_merchant_context(x_session_id)
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        control = store.approval_record(change_id)
        if control is None or control["state"] != "reconciliation_required":
            raise HTTPException(
                status_code=409,
                detail=f"change {change_id} does not require reconciliation",
            )
        change = StagedChange.model_validate(record)
        assessment = await assess_reconciliation(store, change, record.get("payload"))
        return {
            "change": change.model_dump(mode="json"),
            "proposal_digest": record["proposal_digest"],
            "control": control,
            "assessment": assessment.model_dump(mode="json"),
            "events": store.approval_events(change_id),
        }

    @app.post("/merchant/changes/{change_id}/reconciliation/start")
    async def merchant_reconciliation_start(
        change_id: str,
        request: ReconciliationStartRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        change = StagedChange.model_validate(record)
        digest = staging.proposal_digest(change, record.get("payload"))
        if digest != record.get("proposal_digest") or digest != request.proposal_digest:
            raise HTTPException(status_code=409, detail="proposal digest changed")
        try:
            store.recover_stale_approval(
                change_id,
                session.operator,
                digest,
                stale_before=datetime.now(UTC) - timedelta(seconds=stale_apply_seconds),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "change_id": change_id,
            "state": "reconciliation_required",
            "assessment": (
                await assess_reconciliation(store, change, record.get("payload"))
            ).model_dump(mode="json"),
        }

    @app.post("/merchant/changes/{change_id}/reconciliation")
    async def merchant_reconciliation_resolve(
        change_id: str,
        request: ReconciliationRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        record = await staging.load_record(store, change_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        change = StagedChange.model_validate(record)
        digest = staging.proposal_digest(change, record.get("payload"))
        if digest != record.get("proposal_digest") or digest != request.proposal_digest:
            raise HTTPException(status_code=409, detail="proposal digest changed")
        control = store.approval_record(change_id)
        if control is None or control["state"] != "reconciliation_required":
            raise HTTPException(
                status_code=409,
                detail=f"change {change_id} does not require reconciliation",
            )
        try:
            store.claim_reconciliation(
                change_id,
                session.operator,
                digest,
                request.resolution,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            # Assess only after winning the durable reconciliation claim. This avoids
            # persisting a conclusion another operator computed before losing the race.
            assessment = await assess_reconciliation(store, change, record.get("payload"))
            if request.resolution == "confirmed_applied":
                if assessment.outcome != "applied":
                    raise HTTPException(
                        status_code=409,
                        detail="live state does not fully match the approved proposal",
                    )
                resolved = change.model_copy(
                    update={
                        "status": ChangeStatus.APPLIED,
                        "applied_at": datetime.now(UTC),
                        "applied_by": session.operator,
                        "guardrail_notes": [
                            *change.guardrail_notes,
                            "operator reconciled live state as fully applied",
                        ],
                    }
                )
            else:
                resolved = change.model_copy(
                    update={
                        "status": ChangeStatus.DISCARDED,
                        "discarded_at": datetime.now(UTC),
                        "discarded_by": session.operator,
                        "discarded_by_kind": ActorKind.OPERATOR,
                        "guardrail_notes": [
                            *change.guardrail_notes,
                            "operator accepted current live state after ambiguous apply",
                        ],
                    }
                )
            await staging.save(store, resolved)
        except Exception as error:
            store.abort_reconciliation(
                change_id,
                session.operator,
                digest,
                request.resolution,
                str(error),
            )
            raise
        store.finish_reconciliation(
            change_id,
            session.operator,
            digest,
            request.resolution,
        )
        return {
            "change_id": change_id,
            "resolution": request.resolution,
            "assessment": assessment.model_dump(mode="json"),
        }

    @app.get("/merchant/changes")
    async def merchant_changes_read(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Pending and applied changes, each carrying its structured evidence -- a
        sealed kernel receipt id or an activity-log id, read straight from the
        persisted record, never inferred from ``guardrail_notes`` prose. This is a
        read: it introduces no write and calls neither ``approve`` nor ``apply_change``."""
        _bound_merchant_context(x_session_id)
        records = await store.call(lambda c: list_payloads(c, STAGED_TYPE))
        approval_records = store.approval_records(
            [record["change_id"] for record in records if record.get("change_id")]
        )
        approval_events = store.approval_event_records(
            [record["change_id"] for record in records if record.get("change_id")]
        )
        changes = []
        for record in records:
            change = StagedChange.model_validate(record)
            control = approval_records.get(change.change_id)
            operationally_active = control and control["state"] in (
                "applying",
                "reconciliation_required",
                "reconciling",
            )
            if (
                change.status not in (ChangeStatus.STAGED, ChangeStatus.APPLIED)
                and not operationally_active
            ):
                continue
            item = change.model_dump(mode="json")
            item["evidence"] = record.get("evidence") or []
            item["proposal_digest"] = record.get("proposal_digest")
            item["apply_control"] = control
            control = item["apply_control"]
            item["recovery_available_at"] = None
            if control and control["state"] in ("applying", "reconciling"):
                recovery_started_at = (
                    control.get("claimed_at")
                    if control["state"] == "applying"
                    else control.get("resolved_at")
                )
                if recovery_started_at:
                    item["recovery_available_at"] = (
                        datetime.fromisoformat(recovery_started_at)
                        + timedelta(seconds=stale_apply_seconds)
                    ).isoformat()
            item["approval_events"] = approval_events.get(change.change_id, [])
            changes.append(item)
        changes.sort(key=lambda item: item["created_at"])
        return {"changes": changes}

    # -- Merchant: governed refunds -------------------------------------------------

    @app.post("/merchant/refunds/preview")
    async def merchant_refund_preview(
        request: RefundPreviewRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Build the exact proposal an operator must review; performs no write."""
        _bound_merchant_context(x_session_id)
        try:
            result = await refunds.preview(store, request.payment_id, request.amount)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="payment not found") from error
        return result.model_dump(mode="json")

    @app.post("/merchant/refunds")
    async def merchant_refund_apply(
        request: RefundApplyRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Human-only refund path, governed inside the engine transaction.

        No agent or MCP tool reaches this route. The signed HTTP identity supplies the
        operator, while the echoed proposal digest binds approval to the reviewed
        payment and exact amount.
        """
        session = _bound_merchant_context(x_session_id)
        try:
            proposal = await refunds.preview(store, request.payment_id, request.amount)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="payment not found") from error
        if proposal.proposal_digest != request.proposal_digest:
            raise HTTPException(status_code=409, detail="refund proposal digest changed")
        receipt = await kernel.execute(
            "payments.create_refund",
            {"payment_id": proposal.payment_id, "amount": proposal.refund_amount},
            idempotency_key=request.idempotency_key,
            approval=approval_evidence(
                f"refund:{proposal.proposal_digest.removeprefix('sha256:')}",
                session.operator,
                "payments.create_refund",
                store.store_id,
            ),
            correlation_id=http_request.state.request_id,
        )
        metrics.kernel_command("payments.create_refund", receipt.status or "unknown")
        if not receipt.ok:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": receipt.error_code,
                    "error_message": receipt.error_message,
                    "receipt_id": receipt.receipt_id,
                    "sealed": receipt.sealed,
                },
            )
        return {
            "proposal": proposal.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"),
        }

    # -- Capabilities ---------------------------------------------------------------

    @app.get("/capabilities")
    async def capabilities() -> dict[str, str]:
        """Whether a model is configured for this deployment -- present or absent,
        never valid or invalid, since that would require a call to the provider. Not
        session-scoped: a browser needs this before it has a session. Never echoes the
        key or the workspace id; this route never touches either value's contents."""
        return {"assistant": "available" if anthropic_client is not None else "unconfigured"}

    return app


__all__ = ["create_app"]
