"""Everything the route modules share: one engine store, both backends, the kernel
client, both agents, both session registries, metrics, and the settings. Built once by
``create_app`` and closed over by every ``build_router``.

The helpers here are the ones more than one surface needs -- binding a session header
back to a principal, the operator-authority checks, and the cart payload / commit path
that both the direct and the stablecoin checkout share.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from commerce_common.streaming import AgentEvent, to_sse
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from merchant_agent.types import ChangeStatus, MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent
from pydantic import BaseModel
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent
from stateset_embedded import CartAddress

from engine_backend.kernel import KernelClient
from engine_backend.merchant import EngineMerchant
from engine_backend.stablecoins import StablecoinConfig, StablecoinPayments
from engine_backend.staging import load_evidence
from engine_backend.store import EngineStore
from engine_backend.storefront import EngineStorefront

from .auth import Authenticator, Identity
from .metrics import HostMetrics
from .response_policy import TurnResponsePolicy, replace_latest_assistant_text
from .sessions import ChatTurnBusy, ClaimedChat, SessionRegistry
from .settings import HostSettings

logger = logging.getLogger(__name__)

_ROWAN_EMAIL = "rowan@example.invalid"
_OPERATOR_ID = "user:acme-operator"


@dataclass
class HostContext:
    settings: HostSettings
    store: EngineStore
    storefront: EngineStorefront
    kernel: KernelClient
    merchant: EngineMerchant
    authenticator: Authenticator
    stablecoin_config: StablecoinConfig
    stablecoin_payments: StablecoinPayments
    anthropic_client: Any
    shopping_agent: ShoppingAgent
    merchant_agent: MerchantAgent
    shopping_sessions: SessionRegistry[ShoppingSessionState]
    merchant_sessions: SessionRegistry[MerchantSessionState]
    metrics: HostMetrics

    # -- Bindings -----------------------------------------------------------------

    def bound_shopping_context(self, x_session_id: str | None) -> ShoppingSessionContext:
        session_id = _session_id(x_session_id)
        try:
            binding = self.store.binding(session_id)
        except KeyError as error:
            raise HTTPException(status_code=401, detail="unknown session") from error
        if binding.kind != "customer":
            raise HTTPException(status_code=401, detail="not a shopping session")
        return ShoppingSessionContext(session_id=session_id, user_id=binding.subject_id)

    def bound_merchant_context(self, x_session_id: str | None) -> MerchantSessionContext:
        session_id = _session_id(x_session_id)
        try:
            binding = self.store.binding(session_id)
        except KeyError as error:
            raise HTTPException(status_code=401, detail="unknown session") from error
        if binding.kind != "operator":
            raise HTTPException(status_code=401, detail="not a merchant session")
        return MerchantSessionContext(
            session_id=session_id, merchant_id=self.store.store_id, operator=binding.subject_id
        )

    def require_payment_reconciler(self, request: Request) -> None:
        if self.authenticator.config.mode == "demo":
            return
        identity: Identity | None = request.state.identity
        if identity is None or not identity.permits(
            role="merchant_admin", scope="payments:reconcile"
        ):
            raise HTTPException(status_code=403, detail="payment reconciliation access required")

    def require_refund_operator(self, request: Request) -> None:
        if self.authenticator.config.mode == "demo":
            return
        identity: Identity | None = request.state.identity
        if identity is None or not identity.permits(role="merchant_admin", scope="payments:refund"):
            raise HTTPException(status_code=403, detail="payment refund access required")

    # -- Cart payloads and the governed commit ------------------------------------

    async def cart_payload(self, session: ShoppingSessionContext, cart: Any) -> dict[str, Any]:
        payload = cart.model_dump(mode="json")
        # The engine's own exact decimal totals, not a figure recomputed from the
        # ``float`` prices above: the browser displays these as given, never multiplies.
        exact = await self.storefront.cart_exact_totals(session)
        for item in payload["items"]:
            item["total_exact"] = exact["line_totals_exact"].get(item["product_id"])
        payload["subtotal_exact"] = exact["subtotal_exact"]
        payload["grand_total_exact"] = exact["grand_total_exact"]
        return payload

    async def cart_payment_snapshot(
        self, session: ShoppingSessionContext, cart_id: str
    ) -> dict[str, Any]:
        cart = await self.storefront.get_cart(session)
        payload = await self.cart_payload(session, cart)
        return {
            "cart_id": cart_id,
            "currency": payload["currency"],
            "grand_total_exact": payload["grand_total_exact"],
            "items": [
                {
                    "product_id": item["product_id"],
                    "quantity": item["quantity"],
                    "total_exact": item["total_exact"],
                }
                for item in payload["items"]
            ],
        }

    async def commit_cart(
        self,
        *,
        session_id: str,
        cart_id: str,
        address: CartAddress,
        correlation_id: str,
    ) -> dict[str, Any]:
        """The one path that completes an order: ``checkout.commit`` in the kernel."""

        def set_address(c: Any) -> None:
            c.carts.set_shipping_address(cart_id, address)

        await self.store.write(session_id, set_address)
        receipt = await self.kernel.execute(
            "checkout.commit",
            {"cart_id": cart_id},
            idempotency_key=f"checkout-{cart_id}",
            correlation_id=correlation_id,
        )
        self.metrics.kernel_command("checkout.commit", receipt.status or "unknown")
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

    async def complete_settled_payment(
        self,
        payment_id: str,
        payment: dict[str, Any],
        *,
        resume: bool,
        correlation_id: str,
        failure: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Commit the cart behind a settled stablecoin payment and mark it completed.

        Both the shopper's settle route and the operator's reconciliation route end
        here. The payment moves ``settled`` -> ``checkout_committing`` -> ``completed``;
        any failure in between parks it in ``reconciliation_required`` with ``failure``
        as its error and re-raises, so a settled transfer can never be charged twice and
        never silently stays in ``checkout_committing``. ``resume`` lets the shopper
        route re-enter a commit an earlier request started.
        """
        session_id = payment["session_id"]
        from_states = {"settled", "checkout_committing"} if resume else {"settled"}
        payment = await self.stablecoin_payments.transition(
            payment_id, session_id, from_states, "checkout_committing"
        )
        try:
            address_data = json.loads(payment["shipping_address_json"])
            customer_id = payment["customer_id"]
            customer = await self.store.call(lambda c: c.customers.get(customer_id))
            if customer is None:
                raise RuntimeError("payment customer no longer exists")
            checkout_result = await self.commit_cart(
                session_id=session_id,
                cart_id=payment["cart_id"],
                address=build_cart_address(customer.email, **address_data),
                correlation_id=correlation_id,
            )
            payment = await self.stablecoin_payments.transition(
                payment_id,
                session_id,
                {"checkout_committing"},
                "completed",
                order_number=checkout_result["order_number"],
                checkout_receipt_json=json.dumps(
                    checkout_result["receipt"], sort_keys=True, separators=(",", ":")
                ),
                last_error=None,
            )
        except Exception:
            await self.stablecoin_payments.transition(
                payment_id,
                session_id,
                {"checkout_committing"},
                "reconciliation_required",
                last_error=failure,
            )
            raise
        return payment, checkout_result

    # -- Chat turns -----------------------------------------------------------------

    async def claim_chat[StateT: BaseModel](
        self, registry: SessionRegistry[StateT], session_id: str
    ) -> ClaimedChat[StateT]:
        try:
            return await registry.claim(session_id)
        except ChatTurnBusy as error:
            raise HTTPException(
                status_code=409, detail="another chat turn is in progress"
            ) from error

    def stream_chat_turn[StateT: BaseModel](
        self,
        role: Literal["shopping", "merchant"],
        registry: SessionRegistry[StateT],
        claimed: ClaimedChat[StateT],
        events: Callable[[], AsyncIterator[AgentEvent]],
        *,
        message: str,
        request_id: str,
        enrich: Callable[[AgentEvent], Awaitable[AgentEvent]] | None = None,
        after_turn: Callable[[], None] | None = None,
    ) -> StreamingResponse:
        """One SSE turn: append the user message, stream the agent under the turn
        lease, apply the last-mile response policy, then persist and release.

        ``events`` is a factory so the agent generator is created only after the user
        message is on the transcript. ``enrich`` runs per event (the merchant attaches
        change evidence); ``after_turn`` runs once the agent is done, before persistence.
        """
        chat = claimed.session

        async def event_stream() -> AsyncIterator[str]:
            try:
                chat.messages.append({"role": "user", "content": message})
                policy = TurnResponsePolicy(role, message)
                try:
                    async for event in registry.stream(claimed, events()):
                        if enrich is not None:
                            event = await enrich(event)
                        for checked in policy.accept(event):
                            yield to_sse(checked)
                    for checked in policy.flush():
                        yield to_sse(checked)
                    if policy.rewritten:
                        replace_latest_assistant_text(chat.messages, policy.final_text)
                        self.metrics.policy_rewrite(role)
                        logger.warning(
                            "response policy rewrote role=%s request_id=%s", role, request_id
                        )
                finally:
                    if after_turn is not None:
                        after_turn()
            finally:
                await registry.finish(claimed)

        return StreamingResponse(event_stream(), media_type="text/event-stream")


def build_cart_address(email: str | None, **fields: Any) -> CartAddress:
    """Construct the engine's ``CartAddress``.

    The binding's ``.pyi`` declares the class without an ``__init__``, so a direct
    keyword construction is a type error even though the runtime accepts exactly these
    fields. Funnel every construction through here so that stub gap is one line.
    """
    return CartAddress(email=email, **fields)  # type: ignore[call-arg]


def _session_id(x_session_id: str | None) -> str:
    if not x_session_id:
        raise HTTPException(status_code=401, detail="missing X-Session-Id")
    return x_session_id


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
