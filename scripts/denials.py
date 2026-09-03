"""Three refusals, end to end: a cart write the agent layer never lets reach the
engine, an apply the agent layer refuses for lack of approval, and an over-refund the
engine itself refuses inside the transaction even with valid approval evidence.

Each section prints what was attempted, the refusal, and the evidence for it. Exit 0
means all three refusals fired as expected -- a refusal is the success condition here.
Exit non-zero means one of them was *not* refused, which is the failure this script
exists to catch.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from merchant_agent.gates import check_apply_change
from merchant_agent.types import (
    MerchantSessionContext,
    MerchantSessionState,
    PriceUpdateItem,
)
from shopping_agent.gates import check_provenance
from shopping_agent.types import ShoppingSessionState

from engine_backend.kernel import KernelClient, approval_evidence
from engine_backend.merchant import EngineMerchant
from engine_backend.seed import seed_store
from engine_backend.store import EngineStore

CONFIG = Path(__file__).resolve().parent.parent / "config"


async def denial_one_agent_layer_cart_write(store: EngineStore) -> bool:
    """A cart write naming a product id the model never saw. `check_provenance` is the
    same gate `gated_add_to_cart` runs before it ever calls the storefront backend --
    the engine never sees this request."""
    print("Attempting: add_to_cart(TENT-RIDGE-GRN) with no session provenance for it")
    state = ShoppingSessionState()  # seen_products is empty -- the model "never saw" it
    outcome = check_provenance(state, "TENT-RIDGE-GRN")
    if outcome is None:
        print("NOT DENIED: the provenance gate let the write through")
        return False
    print(f"DENIED (agent layer): gate={outcome.blocked!r}")
    print(f"  evidence: {outcome.result_text}")
    return outcome.blocked is not None


async def denial_two_agent_layer_apply_without_approval(
    store: EngineStore, kernel: KernelClient
) -> bool:
    """`apply_change` with no host approval. `check_apply_change` is the same gate the
    executor's `apply_change` tool handler runs before it ever calls
    `EngineMerchant.apply_change` -- the engine never sees this request either."""
    merchant = EngineMerchant(store, kernel)
    session = MerchantSessionContext(
        session_id="denial-2", merchant_id=store.store_id, operator="user:acme-operator"
    )
    print("Attempting: stage a price update, then apply_change with no approval")
    change = await merchant.stage_price_update(
        session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199.00)]
    )
    state = MerchantSessionState()
    state.remember_change(change)  # provenance: the change was staged this session
    outcome = check_apply_change(state, merchant_agent_config(), change.change_id)
    if outcome is None:
        print("NOT DENIED: apply_change was allowed with no approval")
        return False
    print(f"DENIED (agent layer): gate={outcome.blocked!r}")
    print(f"  evidence: {outcome.result_text}")
    return outcome.blocked is not None


def merchant_agent_config():
    from merchant_agent.config import MerchantAgentConfig

    return MerchantAgentConfig()


async def denial_three_engine_over_refund(store: EngineStore, kernel: KernelClient) -> bool:
    """An over-refund issued as a governed command with valid approval evidence. This
    reaches the engine's own transaction -- policy passes because approval is present
    and correctly scoped -- and the engine's own refund logic refuses it because the
    amount exceeds what was captured."""
    payment = store.commerce.payments.list()[0]
    print(
        f"Attempting: payments.create_refund for 10000.00 against payment {payment.id} "
        f"({payment.amount} captured), with valid approval evidence"
    )
    receipt = await kernel.execute(
        "payments.create_refund",
        {"payment_id": payment.id, "amount": "10000.00"},
        idempotency_key="denials-refund-toolarge",
        approval=approval_evidence(
            "appr-denials-1", "user:acme-operator", "payments.create_refund", store.store_id
        ),
    )
    if receipt.ok:
        print("NOT DENIED: the over-refund succeeded")
        return False
    print(f"DENIED (engine, in transaction): status={receipt.status!r}")
    print(f"  error_code: {receipt.error_code}")
    print(f"  receipt_id: {receipt.receipt_id}")
    print(f"  evidence: {receipt.error_message}")
    return not receipt.ok


async def main(db_path: str) -> int:
    store = EngineStore(db_path)
    seed_store(store.commerce)
    kernel = KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")

    results = []
    print("=" * 72)
    results.append(await denial_one_agent_layer_cart_write(store))
    print("-" * 72)
    results.append(await denial_two_agent_layer_apply_without_approval(store, kernel))
    print("-" * 72)
    results.append(await denial_three_engine_over_refund(store, kernel))
    print("=" * 72)

    if all(results):
        print("All three denials fired as expected.")
        return 0
    print("One or more denials did NOT fire -- see NOT DENIED lines above.")
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="denials-demo.db", help="path to the store db file")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(asyncio.run(main(args.db)))
