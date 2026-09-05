"""The x402 v2 stablecoin rail: quote, verify/settle, status, and operator reconciliation."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from stateset_embedded import CartAddress

from engine_backend.stablecoins import (
    FacilitatorUncertain,
    PaymentConflict,
    PaymentNotFound,
    public_payment,
)

from ..context import HostContext
from ..schemas import (
    StablecoinQuoteRequest,
    StablecoinReconciliationRequest,
    StablecoinSettleRequest,
)

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    storefront = ctx.storefront
    stablecoin_config = ctx.stablecoin_config
    stablecoin_payments = ctx.stablecoin_payments
    metrics = ctx.metrics
    _bound_shopping_context = ctx.bound_shopping_context
    _bound_merchant_context = ctx.bound_merchant_context
    _require_payment_reconciler = ctx.require_payment_reconciler
    _cart_payment_snapshot = ctx.cart_payment_snapshot
    _commit_cart = ctx.commit_cart

    @router.post("/shopping/checkout/stablecoin/quote")
    async def stablecoin_quote(
        request: StablecoinQuoteRequest,
        x_session_id: str | None = Header(default=None),
    ) -> JSONResponse:
        """Freeze this session's cart and return a standard x402 v2 payment challenge."""
        if not stablecoin_config.enabled:
            raise HTTPException(status_code=404, detail="stablecoin checkout is not configured")
        session = _bound_shopping_context(x_session_id)
        binding = store.binding(session.session_id)
        cart_id = storefront.session_cart_id(session.session_id)
        if cart_id is None:
            raise HTTPException(status_code=409, detail="no cart to check out")
        snapshot = await _cart_payment_snapshot(session, cart_id)
        if not snapshot["items"] or snapshot["grand_total_exact"] is None:
            raise HTTPException(status_code=409, detail="cart is empty")
        customer = await store.call(lambda c: c.customers.get(binding.subject_id))
        shipping = request.shipping_address.model_dump(mode="json")
        try:
            quote = await stablecoin_payments.quote(
                session_id=session.session_id,
                customer_id=binding.subject_id,
                store_id=binding.store_id,
                cart_id=cart_id,
                cart_snapshot=snapshot,
                shipping_address=shipping,
                payer_address=request.payer_address,
            )
        except PaymentConflict as error:
            metrics.stablecoin_payment("quote", "conflict")
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            metrics.stablecoin_payment("quote", "rejected")
            raise HTTPException(status_code=422, detail=str(error)) from error
        # The email comes from the authenticated server-side customer record at commit
        # time; it is intentionally absent from the public payment challenge.
        assert customer is not None
        encoded = base64.b64encode(
            json.dumps(quote["payment_required"], separators=(",", ":")).encode()
        ).decode()
        metrics.stablecoin_payment("quote", "required")
        body = {
            **quote["payment_required"],
            "paymentId": quote["payment_id"],
            "quoteDigest": quote["quote_digest"],
            "expiresAt": quote["expires_at"],
        }
        return JSONResponse(
            status_code=402,
            content=body,
            headers={"PAYMENT-REQUIRED": encoded},
        )

    @router.post("/shopping/checkout/stablecoin/{payment_id}")
    async def stablecoin_checkout(
        payment_id: str,
        request: StablecoinSettleRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
        payment_signature: str | None = Header(default=None, alias="PAYMENT-SIGNATURE"),
    ) -> JSONResponse:
        """Verify, settle, then idempotently commit the cart represented by a quote."""
        if not stablecoin_config.enabled:
            raise HTTPException(status_code=404, detail="stablecoin checkout is not configured")
        session = _bound_shopping_context(x_session_id)
        if payment_signature is None:
            raise HTTPException(status_code=402, detail="missing PAYMENT-SIGNATURE")
        try:
            before = await stablecoin_payments.get(payment_id, session.session_id)
            current_cart_id = storefront.session_cart_id(session.session_id)
            if current_cart_id != before["cart_id"]:
                raise PaymentConflict("quoted cart is no longer attached to this session")
            snapshot = await _cart_payment_snapshot(session, before["cart_id"])
            payment = await stablecoin_payments.verify_and_settle(
                payment_id=payment_id,
                session_id=session.session_id,
                quote_digest=request.quote_digest,
                payment_signature=payment_signature,
                current_cart_snapshot=snapshot,
            )
        except PaymentNotFound as error:
            raise HTTPException(status_code=404, detail="payment not found") from error
        except ValueError as error:
            metrics.stablecoin_payment("verify", "invalid")
            raise HTTPException(status_code=422, detail=str(error)) from error
        except FacilitatorUncertain:
            payment = await stablecoin_payments.get(payment_id, session.session_id)
            action = "settle" if payment["state"] == "reconciliation_required" else "verify"
            metrics.stablecoin_payment(action, "unknown")
            status = 202 if payment["state"] == "reconciliation_required" else 503
            return JSONResponse(status_code=status, content=public_payment(payment))
        except (PaymentConflict, sqlite3.IntegrityError) as error:
            metrics.stablecoin_payment("settle", "rejected")
            raise HTTPException(status_code=409, detail=str(error)) from error

        if payment["state"] == "completed":
            metrics.stablecoin_payment("checkout", "idempotent")
            response = public_payment(payment)
        else:
            try:
                payment = await stablecoin_payments.transition(
                    payment_id,
                    session.session_id,
                    {"settled", "checkout_committing"},
                    "checkout_committing",
                )
                address_data = json.loads(payment["shipping_address_json"])
                customer = await store.call(lambda c: c.customers.get(payment["customer_id"]))
                address = CartAddress(email=customer.email, **address_data)
                checkout_result = await _commit_cart(
                    session_id=session.session_id,
                    cart_id=payment["cart_id"],
                    address=address,
                    correlation_id=http_request.state.request_id,
                )
                payment = await stablecoin_payments.transition(
                    payment_id,
                    session.session_id,
                    {"checkout_committing"},
                    "completed",
                    order_number=checkout_result["order_number"],
                    checkout_receipt_json=json.dumps(
                        checkout_result["receipt"], sort_keys=True, separators=(",", ":")
                    ),
                    last_error=None,
                )
                response = {**public_payment(payment), **checkout_result}
                metrics.stablecoin_payment("checkout", "completed")
            except Exception as error:
                await stablecoin_payments.transition(
                    payment_id,
                    session.session_id,
                    {"checkout_committing"},
                    "reconciliation_required",
                    last_error="stablecoin settled but checkout commit did not complete",
                )
                metrics.stablecoin_payment("checkout", "reconciliation_required")
                raise HTTPException(
                    status_code=202,
                    detail="payment settled; checkout requires reconciliation",
                ) from error
        settlement_evidence = {
            "success": True,
            "transaction": payment["transaction_hash"],
            "network": payment["network"],
            "payer": payment["payer_address"],
        }
        encoded = base64.b64encode(
            json.dumps(settlement_evidence, separators=(",", ":")).encode()
        ).decode()
        return JSONResponse(content=response, headers={"PAYMENT-RESPONSE": encoded})

    @router.get("/shopping/payments/{payment_id}")
    async def stablecoin_payment_status(
        payment_id: str,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_shopping_context(x_session_id)
        try:
            payment = await stablecoin_payments.get(payment_id, session.session_id)
        except PaymentNotFound as error:
            raise HTTPException(status_code=404, detail="payment not found") from error
        return public_payment(payment)

    @router.get("/merchant/stablecoin-payments")
    async def stablecoin_reconciliation_queue(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Recent completed payments and any transfer needing operator recovery."""
        if not stablecoin_config.enabled:
            raise HTTPException(status_code=404, detail="stablecoin checkout is not configured")
        session = _bound_merchant_context(x_session_id)
        payments = await stablecoin_payments.list_for_operator(session.merchant_id)
        return {"payments": [public_payment(payment) for payment in payments]}

    @router.post("/merchant/stablecoin-payments/{payment_id}/reconcile")
    async def reconcile_stablecoin_payment(
        payment_id: str,
        request: StablecoinReconciliationRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Record externally verified chain truth; never infer it from a timeout."""
        if not stablecoin_config.enabled:
            raise HTTPException(status_code=404, detail="stablecoin checkout is not configured")
        operator = _bound_merchant_context(x_session_id)
        _require_payment_reconciler(http_request)
        try:
            payment = await stablecoin_payments.get_for_operator(payment_id, operator.merchant_id)
            if payment["state"] == "completed":
                return public_payment(payment)
            if payment["state"] != "reconciliation_required":
                raise PaymentConflict(f"payment is {payment['state']}")
            if request.resolution == "confirmed_not_settled":
                if payment["transaction_hash"] is not None:
                    raise PaymentConflict(
                        "a recorded settlement transaction cannot be marked not settled"
                    )
                payment = await stablecoin_payments.transition(
                    payment_id,
                    payment["session_id"],
                    {"reconciliation_required"},
                    "failed",
                    event="operator_confirmed_not_settled",
                    event_detail=f"{operator.operator}: {request.note}",
                    last_error="operator confirmed that settlement did not occur",
                )
                metrics.stablecoin_payment("reconcile", "confirmed_not_settled")
                return public_payment(payment)

            transaction_hash = payment["transaction_hash"] or request.transaction_hash
            if transaction_hash is None:
                raise ValueError(
                    "transaction_hash is required when confirming an unknown settlement"
                )
            payment = await stablecoin_payments.transition(
                payment_id,
                payment["session_id"],
                {"reconciliation_required"},
                "settled",
                event="operator_confirmed_settled",
                event_detail=f"{operator.operator}: {request.note}",
                transaction_hash=transaction_hash.lower(),
                last_error="operator confirmed settlement from external evidence",
            )
            payment = await stablecoin_payments.transition(
                payment_id,
                payment["session_id"],
                {"settled"},
                "checkout_committing",
            )
            address_data = json.loads(payment["shipping_address_json"])
            customer = await store.call(lambda c: c.customers.get(payment["customer_id"]))
            checkout_result = await _commit_cart(
                session_id=payment["session_id"],
                cart_id=payment["cart_id"],
                address=CartAddress(email=customer.email, **address_data),
                correlation_id=http_request.state.request_id,
            )
            payment = await stablecoin_payments.transition(
                payment_id,
                payment["session_id"],
                {"checkout_committing"},
                "completed",
                order_number=checkout_result["order_number"],
                checkout_receipt_json=json.dumps(
                    checkout_result["receipt"], sort_keys=True, separators=(",", ":")
                ),
                last_error=None,
            )
            metrics.stablecoin_payment("reconcile", "completed")
            return {**public_payment(payment), **checkout_result}
        except PaymentNotFound as error:
            raise HTTPException(status_code=404, detail="payment not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (PaymentConflict, sqlite3.IntegrityError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except HTTPException as error:
            current = await stablecoin_payments.get_for_operator(payment_id, operator.merchant_id)
            if current["state"] == "checkout_committing":
                await stablecoin_payments.transition(
                    payment_id,
                    current["session_id"],
                    {"checkout_committing"},
                    "reconciliation_required",
                    last_error="settlement confirmed but checkout commit did not complete",
                )
            raise error

    return router
