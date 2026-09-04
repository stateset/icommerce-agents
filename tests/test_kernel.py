import json
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


async def test_a_sealed_receipt_is_distinguishable_from_a_synthesized_one(kernel, store):
    sealed = await kernel.execute(
        "payments.create",
        {"amount": "1.00", "currency": "USD", "payment_method": "credit_card"},
        idempotency_key="test-sealed-1",
    )
    assert sealed.sealed is True

    del kernel.policy["version"]  # KernelPolicy.version has no serde default -> ValueError
    unsealed = await kernel.execute(
        "payments.create",
        {"amount": "1.00", "currency": "USD", "payment_method": "credit_card"},
        idempotency_key="test-unsealed-1",
    )
    assert unsealed.sealed is False
    assert not unsealed.ok
    assert unsealed.error_code == "kernel.rejected"


async def test_a_non_kernel_exception_propagates_rather_than_becoming_a_receipt(
    kernel, store, monkeypatch
):
    async def boom(session_key, fn):
        raise AttributeError("simulated programming defect")

    monkeypatch.setattr(store, "write", boom)

    with pytest.raises(AttributeError, match="simulated programming defect"):
        await kernel.execute(
            "payments.create",
            {"amount": "1.00", "currency": "USD", "payment_method": "credit_card"},
            idempotency_key="test-propagate-1",
        )


async def test_request_trace_fields_are_carried_into_the_kernel_envelope(
    kernel, store, monkeypatch
):
    captured = {}

    async def capture(_session_key, body):
        class CommerceStub:
            def execute_kernel_command(self, command_json, _policy_json):
                captured.update(json.loads(command_json))
                return json.dumps(
                    {
                        "receipt_id": "receipt-trace",
                        "command_id": captured["command_id"],
                        "command_type": captured["command_type"],
                        "status": "succeeded",
                        "idempotency_key": captured["idempotency_key"],
                        "sealed": True,
                    }
                )

        return body(CommerceStub())

    monkeypatch.setattr(store, "write", capture)
    receipt = await kernel.execute(
        "payments.create",
        {"amount": "1.00", "currency": "USD", "payment_method": "credit_card"},
        idempotency_key="trace-payment-1",
        correlation_id="request-123",
        causation_id="change-456",
        trace_id="trace-789",
    )
    assert receipt.ok
    assert captured["correlation_id"] == "request-123"
    assert captured["causation_id"] == "change-456"
    assert captured["trace_id"] == "trace-789"


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


def test_every_enabled_policy_command_is_disclosed_in_the_enforcement_doc():
    """`docs/enforcement.md` states, per command, whether anything in this repo can
    reach it. Two of the five grants have no code path at all; that is disclosed rather
    than hidden, and this test is what stops a grant added later from going undisclosed.
    """
    policy = json.loads((CONFIG / "kernel-policy.json").read_text())
    doc = (CONFIG.parent / "docs" / "enforcement.md").read_text()
    undisclosed = [name for name in policy["commands"] if f"`{name}`" not in doc]
    assert not undisclosed, (
        f"kernel-policy.json enables {undisclosed} but docs/enforcement.md never names "
        "them; every grant must say where it is issued from, or that nothing issues it"
    )


def test_the_commands_with_no_code_path_are_named_as_such():
    """The specific claim the doc makes: `payments.create` is issued only by a test and
    `products.create` by nothing at all. If either becomes reachable, this fails and the
    table needs rewriting rather than quietly going stale."""
    repo = CONFIG.parent
    # Everything a running deployment executes: the backends, the host, the MCP servers
    # and the scripts. Not `tests/`, which is where the doc says these two are issued.
    issuers = set()
    for package in ("engine_backend", "host", "mcp_servers", "scripts", "evals"):
        for path in sorted((repo / package).glob("*.py")):
            source = path.read_text()
            for command in ('"payments.create"', '"products.create"'):
                if command in source and "kernel.execute" in source.replace("self.", ""):
                    issuers.add((f"{package}/{path.name}", command))
    assert not issuers, (
        "docs/enforcement.md says nothing outside tests/ issues these as kernel "
        f"commands; found {sorted(issuers)}"
    )
