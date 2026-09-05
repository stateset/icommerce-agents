import asyncio
import multiprocessing
import os
import signal
from datetime import UTC, datetime, timedelta

import pytest
from merchant_agent.types import MerchantSessionContext, PriceUpdateItem

from engine_backend.merchant import EngineMerchant
from engine_backend.store import EngineStore, MerchantOperationBusy

DIGEST = "sha256:" + "a" * 64


def _hold_apply(path, ready):
    store = EngineStore(path)
    with store.merchant_operation("change"):
        claim = store.claim_approval("change", "operator", DIGEST, ["sku"])
        ready.send(claim.attempt_id)
        signal.pause()


def test_paused_apply_cannot_be_recovered_until_worker_exits(tmp_path):
    path = str(tmp_path / "store.db")
    store = EngineStore(path)
    store.record_approval("change", "operator", DIGEST)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_apply, args=(path, child))
    process.start()
    try:
        assert parent.poll(20)
        assert parent.recv()
        os.kill(process.pid, signal.SIGSTOP)
        # Force the age predicate to pass; OS ownership must still prevent recovery.
        stale_before = datetime.now(UTC) + timedelta(seconds=1)
        with pytest.raises(MerchantOperationBusy, match="still active"):
            store.recover_stale_approval("change", "reviewer", DIGEST, stale_before=stale_before)
        assert store.approval_record("change")["state"] == "applying"
        process.kill()
        process.join(10)
        assert not process.is_alive()
        store.recover_stale_approval("change", "reviewer", DIGEST, stale_before=stale_before)
        assert store.approval_record("change")["state"] == "reconciliation_required"
        # Crash recovery must retain target leases, not permit another mutation.
        store.record_approval("other", "operator", DIGEST)
        assert (
            store.claim_approval("other", "operator", DIGEST, ["sku"]).refusal == "target_claimed"
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()


@pytest.mark.parametrize("memory", [True, False])
def test_resolution_guard_excludes_recovery_and_other_owners(tmp_path, memory):
    path = ":memory:" if memory else str(tmp_path / "store.db")
    store = EngineStore(path)
    other = store if memory else EngineStore(path)
    with store.merchant_operation("change"):
        with pytest.raises(MerchantOperationBusy):
            with other.merchant_operation("change"):
                pytest.fail("two owners entered")
        with other.merchant_operation("different"):
            pass
    with other.merchant_operation("change"):
        pass


async def test_real_apply_retains_ownership_through_final_write(store, kernel, monkeypatch):
    import engine_backend.merchant as module

    backend = EngineMerchant(store, kernel)
    session = MerchantSessionContext(
        session_id="merchant", merchant_id=store.store_id, operator="operator"
    )
    change = await backend.stage_price_update(
        session, [PriceUpdateItem(listing_id="TENT-RIDGE-TAN", new_price=199)]
    )
    backend.approve(change.change_id, "operator")
    digest = store.approval_record(change.change_id)["proposal_digest"]
    started = asyncio.Event()
    release = asyncio.Event()
    original = module._apply_change

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "_apply_change", delayed)
    task = asyncio.create_task(backend.apply_change(session, change.change_id))
    await asyncio.wait_for(started.wait(), 5)
    try:
        with pytest.raises(MerchantOperationBusy):
            store.recover_stale_approval(
                change.change_id,
                "reviewer",
                digest,
                stale_before=datetime.now(UTC) + timedelta(seconds=1),
            )
    finally:
        release.set()
        await task
    assert store.approval_record(change.change_id)["state"] == "applied"
    with store.merchant_operation(change.change_id):
        pass
