"""Canonical, reviewable refund proposals for the human operator surface."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from engine_backend import money
from engine_backend.store import EngineStore


class RefundPreview(BaseModel):
    payment_id: str
    payment_number: str
    order_id: str | None
    currency: str
    captured_amount: str
    refund_amount: str
    proposal_digest: str
    note: str = "The engine determines the final refundable balance inside the transaction."


def proposal_digest(store_id: str, payment_id: str, amount: str) -> str:
    canonical = json.dumps(
        {
            "version": "refund-proposal-v1",
            "store_id": store_id,
            "payment_id": payment_id,
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


async def preview(store: EngineStore, payment_id: str, amount: Decimal) -> RefundPreview:
    amount_exact = money.exact(amount)
    if Decimal(amount_exact) <= 0:
        raise ValueError("refund amount must be positive")
    try:
        normalized_payment_id = str(UUID(payment_id))
    except ValueError as error:
        raise KeyError(payment_id) from error
    payment = await store.call(lambda commerce: commerce.payments.get(normalized_payment_id))
    if payment is None:
        raise KeyError(payment_id)
    return RefundPreview(
        payment_id=payment.id,
        payment_number=payment.payment_number,
        order_id=payment.order_id,
        currency=payment.currency,
        captured_amount=money.exact(payment.amount_exact),
        refund_amount=amount_exact,
        proposal_digest=proposal_digest(store.store_id, payment.id, amount_exact),
    )


__all__ = ["RefundPreview", "preview", "proposal_digest"]
