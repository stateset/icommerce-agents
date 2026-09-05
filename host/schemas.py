"""Request bodies the host accepts. Identity is never a field here: every route reads
the principal back from the ``X-Session-Id`` binding the host minted."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


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


class StablecoinQuoteRequest(BaseModel):
    shipping_address: ShippingAddressRequest
    payer_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class StablecoinSettleRequest(BaseModel):
    quote_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class StablecoinReconciliationRequest(BaseModel):
    resolution: Literal["confirmed_settled", "confirmed_not_settled"]
    transaction_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    note: str = Field(min_length=8, max_length=500)


class RefundPreviewRequest(BaseModel):
    payment_id: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)


class RefundApplyRequest(RefundPreviewRequest):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


class StablecoinRefundReconciliationRequest(BaseModel):
    resolution: Literal["confirmed_refunded", "confirmed_not_refunded"]
    transaction_hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    note: str = Field(min_length=8, max_length=500)


class ReconciliationRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolution: Literal["confirmed_applied", "accepted_current_state"]


class ApprovalRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReconciliationStartRequest(BaseModel):
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
