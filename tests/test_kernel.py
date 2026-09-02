from pathlib import Path

import pytest

from engine_backend.kernel import KernelClient, approval_evidence

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")


async def test_a_governed_payment_returns_a_sealed_receipt(kernel, store):
    receipt = await kernel.execute(
        "payments.create",
        {"amount": "12.34", "currency": "USD", "payment_method": "credit_card"},
        idempotency_key="test-payment-1",
    )
    assert receipt.ok
    assert receipt.command_type == "payments.create"
    assert receipt.idempotency_key == "test-payment-1"
    assert receipt.receipt_id


async def test_a_refund_without_approval_evidence_is_refused(kernel, store):
    payment = store.commerce.payments.list()[0]
    receipt = await kernel.execute(
        "payments.create_refund",
        {"payment_id": payment.id, "amount": "10.00"},
        idempotency_key="test-refund-noapproval",
    )
    assert not receipt.ok
    assert receipt.error_code


async def test_an_over_refund_is_refused_even_with_approval(kernel, store):
    payment = store.commerce.payments.list()[0]
    receipt = await kernel.execute(
        "payments.create_refund",
        {"payment_id": payment.id, "amount": "10000.00"},
        idempotency_key="test-refund-toolarge",
        approval=approval_evidence(
            "appr-1", "user:acme-operator", "payments.create_refund", "store:acme"
        ),
    )
    assert not receipt.ok
    assert receipt.error_code
    assert receipt.error_message
