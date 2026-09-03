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

import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from commerce_common.streaming import AgentEvent, to_sse
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from merchant_agent.types import ChangeStatus, MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent
from pydantic import BaseModel
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent
from stateset_embedded import CartAddress

from engine_backend import SKILLS_DIR
from engine_backend.kernel import KernelClient
from engine_backend.merchant import EngineMerchant
from engine_backend.seed import seed_store
from engine_backend.staging import load as load_staged_change
from engine_backend.staging import load_evidence
from engine_backend.store import EngineStore
from engine_backend.storefront import EngineStorefront

from .sessions import SessionRegistry

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_ROWAN_EMAIL = "rowan@example.invalid"
_OPERATOR_ID = "user:acme-operator"


class ChatTurnRequest(BaseModel):
    message: str


class CartAddRequest(BaseModel):
    product_id: str
    quantity: int = 1


# ``engine_backend.apply`` records one of two evidence shapes at apply time: a sealed
# kernel receipt id for a governed write, or an activity-log id for one it only logged.
# ``engine_backend.staging`` persists that as a structured ``Evidence`` list alongside
# the change, keyed by ``change_id`` -- never inferred from ``guardrail_notes`` prose, so
# a wording change to a note cannot make evidence disappear from the portal.
async def _with_change_evidence(store: EngineStore, event: AgentEvent) -> AgentEvent:
    if event.type != "change_update":
        return event
    change = dict(event.data.get("change") or {})
    evidence = await load_evidence(store, change["change_id"])
    change["evidence"] = [item.model_dump() for item in evidence]
    return AgentEvent(type=event.type, data={**event.data, "change": change})


def _session_id(x_session_id: str | None) -> str:
    if not x_session_id:
        raise HTTPException(status_code=401, detail="missing X-Session-Id")
    return x_session_id


def create_app(db_path: str) -> FastAPI:
    """Build one deployment: one engine store (seeded), both backends, one kernel
    client bound to the host-owned policy and principal files, and both agents."""
    store = EngineStore(db_path)
    seed_store(store.commerce)

    storefront = EngineStorefront(store)
    kernel = KernelClient(
        store, CONFIG_DIR / "kernel-policy.json", CONFIG_DIR / "kernel-principal.json"
    )
    merchant = EngineMerchant(store, kernel)

    shopping_agent = ShoppingAgent(backend=storefront, skills_dir=SKILLS_DIR("shopping"))
    merchant_agent = MerchantAgent(backend=merchant, skills_dir=SKILLS_DIR("merchant"))

    shopping_sessions: SessionRegistry[ShoppingSessionState] = SessionRegistry(ShoppingSessionState)
    merchant_sessions: SessionRegistry[MerchantSessionState] = SessionRegistry(MerchantSessionState)

    app = FastAPI(title="StateSet iCommerce agents host")

    # -- Health -----------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
    async def start_shopping_session() -> dict[str, str]:
        customer = store.commerce.customers.get_by_email(_ROWAN_EMAIL)
        session_id = secrets.token_urlsafe(24)
        store.bind(session_id, customer.id, "customer")
        shopping_sessions.start(session_id)
        return {"session_id": session_id}

    @app.post("/merchant/session")
    async def start_merchant_session() -> dict[str, str]:
        session_id = secrets.token_urlsafe(24)
        store.bind(session_id, _OPERATOR_ID, "operator")
        merchant_sessions.start(session_id)
        return {"session_id": session_id}

    # -- Shopping: chat, cart, checkout --------------------------------------------

    @app.post("/shopping/chat")
    async def shopping_chat(
        request: ChatTurnRequest, x_session_id: str | None = Header(default=None)
    ) -> StreamingResponse:
        session = _bound_shopping_context(x_session_id)
        chat = shopping_sessions.require(session.session_id)
        chat.messages.append({"role": "user", "content": request.message})

        async def event_stream() -> AsyncIterator[str]:
            async for event in shopping_agent.stream_turn(chat.messages, session, chat.state):
                yield to_sse(event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/shopping/cart/add")
    async def shopping_cart_add(
        request: CartAddRequest, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        session = _bound_shopping_context(x_session_id)
        cart = await storefront.add_to_cart(session, request.product_id, request.quantity)
        payload = cart.model_dump(mode="json")
        # The engine's own exact decimal totals, not a figure recomputed from the
        # ``float`` prices above: the browser displays these as given, never multiplies.
        exact = await storefront.cart_exact_totals(session)
        for item in payload["items"]:
            item["total_exact"] = exact["line_totals_exact"].get(item["product_id"])
        payload["subtotal_exact"] = exact["subtotal_exact"]
        payload["grand_total_exact"] = exact["grand_total_exact"]
        return payload

    @app.post("/shopping/checkout")
    async def shopping_checkout(x_session_id: str | None = Header(default=None)) -> dict[str, Any]:
        """The only route that completes an order. Reached by a human click, never by
        the model: no agent tool calls this. Governed by ``checkout.commit``.

        Writes a fixed, fictional placeholder shipping address onto the cart first --
        a demo stand-in for the address-collection step a real deployment would run
        before checkout, present only because the engine's checkout-readiness check
        requires one."""
        session = _bound_shopping_context(x_session_id)
        binding = store.binding(session.session_id)
        # This session's own cart, not the customer's most recent one: every shopping
        # session binds to the same seeded customer here, so picking the customer's last
        # cart would let two concurrent sessions check out each other's.
        cart_id = storefront.session_cart_id(session.session_id)
        if cart_id is None:
            raise HTTPException(status_code=409, detail="no cart to check out")
        customer = store.commerce.customers.get(binding.subject_id)

        # DEMO PLACEHOLDER: the engine's checkout-readiness check requires a shipping
        # address on the cart, and this host has no address-collection step yet (a real
        # deployment collects one from the shopper before checkout). This is a fixed,
        # unmistakably fictional ACME Supply placeholder standing in for that step --
        # not a real address, and not one the customer gave -- and exists only to
        # satisfy the engine's readiness check ahead of ``checkout.commit``.
        def set_address(c: Any) -> None:
            c.carts.set_shipping_address(
                cart_id,
                CartAddress(
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
                ),
            )

        await store.write(session.session_id, set_address)
        receipt = await kernel.execute(
            "checkout.commit", {"cart_id": cart_id}, idempotency_key=f"checkout-{cart_id}"
        )
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
        request: ChatTurnRequest, x_session_id: str | None = Header(default=None)
    ) -> StreamingResponse:
        session = _bound_merchant_context(x_session_id)
        chat = merchant_sessions.require(session.session_id)
        chat.messages.append({"role": "user", "content": request.message})

        async def event_stream() -> AsyncIterator[str]:
            async for event in merchant_agent.stream_turn(chat.messages, session, chat.state):
                yield to_sse(await _with_change_evidence(store, event))

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/merchant/changes/{change_id}/approve")
    async def merchant_approve_change(
        change_id: str, x_session_id: str | None = Header(default=None)
    ) -> dict[str, Any]:
        """The operator's approval, and the only place it happens. The operator comes
        from the session binding, never from the request body."""
        session = _bound_merchant_context(x_session_id)
        change = await load_staged_change(store, change_id)
        if change is None:
            raise HTTPException(status_code=404, detail=f"no change with id {change_id!r}")
        if change.status is not ChangeStatus.STAGED:
            # An already-applied or discarded change has nothing left to approve;
            # accepting one would put a live change id into `approved_ids`.
            raise HTTPException(
                status_code=409,
                detail=f"change {change_id} is {change.status.value}, not staged",
            )
        merchant.approve(change_id, session.operator)
        return {"change_id": change_id, "approved_by": session.operator}

    return app


__all__ = ["create_app"]
