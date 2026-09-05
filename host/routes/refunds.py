"""Human-only refund routes: engine refunds governed by ``payments.create_refund`` and
stablecoin refunds through the configured treasury adapter. No agent or MCP tool
reaches any of these."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from engine_backend import refunds
from engine_backend.kernel import approval_evidence
from engine_backend.stablecoins import (
    PaymentConflict,
    PaymentNotFound,
    RefundNotFound,
    RefundUncertain,
    public_refund,
)

from ..context import HostContext
from ..schemas import (
    RefundApplyRequest,
    RefundPreviewRequest,
    StablecoinRefundReconciliationRequest,
)

logger = logging.getLogger(__name__)


def build_router(ctx: HostContext) -> APIRouter:
    router = APIRouter()
    store = ctx.store
    kernel = ctx.kernel
    stablecoin_payments = ctx.stablecoin_payments
    metrics = ctx.metrics
    _bound_merchant_context = ctx.bound_merchant_context
    _require_payment_reconciler = ctx.require_payment_reconciler
    _require_refund_operator = ctx.require_refund_operator

    @router.post("/merchant/stablecoin-refunds/preview")
    async def merchant_stablecoin_refund_preview(
        request: RefundPreviewRequest,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Build a digest-bound on-chain refund proposal without moving funds."""
        session = _bound_merchant_context(x_session_id)
        if not stablecoin_payments.refunds_available:
            raise HTTPException(status_code=404, detail="stablecoin refunds are not configured")
        try:
            return await stablecoin_payments.preview_refund(
                request.payment_id, session.merchant_id, request.amount
            )
        except PaymentNotFound as error:
            raise HTTPException(status_code=404, detail="stablecoin payment not found") from error
        except (PaymentConflict, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/merchant/stablecoin-refunds")
    async def merchant_stablecoin_refund_apply(
        request: RefundApplyRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> JSONResponse:
        """Human-only, idempotent refund through the configured treasury adapter."""
        session = _bound_merchant_context(x_session_id)
        _require_refund_operator(http_request)
        if not stablecoin_payments.refunds_available:
            raise HTTPException(status_code=404, detail="stablecoin refunds are not configured")
        try:
            refund = await stablecoin_payments.refund(
                payment_id=request.payment_id,
                store_id=session.merchant_id,
                amount=request.amount,
                proposal_digest=request.proposal_digest,
                idempotency_key=request.idempotency_key,
                operator=session.operator,
            )
        except PaymentNotFound as error:
            raise HTTPException(status_code=404, detail="stablecoin payment not found") from error
        except RefundUncertain as error:
            metrics.stablecoin_payment("refund", "reconciliation_required")
            assert error.refund is not None
            return JSONResponse(status_code=202, content=public_refund(error.refund))
        except (PaymentConflict, ValueError) as error:
            metrics.stablecoin_payment("refund", "rejected")
            raise HTTPException(status_code=409, detail=str(error)) from error
        outcome = "completed" if refund["state"] == "completed" else refund["state"]
        metrics.stablecoin_payment("refund", outcome)
        status = 202 if refund["state"] in {"submitting", "reconciliation_required"} else 200
        return JSONResponse(status_code=status, content=public_refund(refund))

    @router.get("/merchant/stablecoin-refunds")
    async def merchant_stablecoin_refund_list(
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        if not stablecoin_payments.refunds_available:
            raise HTTPException(status_code=404, detail="stablecoin refunds are not configured")
        items = await stablecoin_payments.list_refunds(session.merchant_id)
        return {"refunds": [public_refund(item) for item in items]}

    @router.post("/merchant/stablecoin-refunds/{refund_id}/reconcile")
    async def merchant_stablecoin_refund_reconcile(
        refund_id: str,
        request: StablecoinRefundReconciliationRequest,
        http_request: Request,
        x_session_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        session = _bound_merchant_context(x_session_id)
        _require_payment_reconciler(http_request)
        if not stablecoin_payments.refunds_available:
            raise HTTPException(status_code=404, detail="stablecoin refunds are not configured")
        try:
            refund = await stablecoin_payments.reconcile_refund(
                refund_id,
                session.merchant_id,
                refunded=request.resolution == "confirmed_refunded",
                transaction_hash=request.transaction_hash,
                note=f"{session.operator}: {request.note}",
            )
            metrics.stablecoin_payment("refund_reconcile", refund["state"])
            return public_refund(refund)
        except RefundNotFound as error:
            raise HTTPException(status_code=404, detail="stablecoin refund not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except PaymentConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post("/merchant/refunds/preview")
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

    @router.post("/merchant/refunds")
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
        _require_refund_operator(http_request)
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

    return router
