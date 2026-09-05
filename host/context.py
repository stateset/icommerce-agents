"""Everything the route modules share: one engine store, both backends, the kernel
client, both agents, both session registries, metrics, and the settings. Built once by
``create_app`` and closed over by every ``build_router``.

The helpers here are the ones more than one surface needs -- binding a session header
back to a principal, the operator-authority checks, and the cart payload / commit path
that both the direct and the stablecoin checkout share.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from commerce_common.streaming import AgentEvent
from fastapi import HTTPException, Request
from merchant_agent.types import ChangeStatus, MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent
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
from .sessions import SessionRegistry
from .settings import HostSettings

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
