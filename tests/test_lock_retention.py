"""Idle lock keys retire without splitting ownership between queued callers."""

import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engine_backend.store import EngineStore, MerchantOperationBusy


async def test_completed_session_operations_do_not_retain_lock_keys():
    store = EngineStore(":memory:")
    for index in range(1000):
        async with store.serialized(f"session-{index}"):
            assert len(store._locks) == 1
    gc.collect()
    assert not store._locks


async def test_waiters_keep_the_same_lock_through_owner_handoff():
    store = EngineStore(":memory:")
    first_waiting = asyncio.Event()
    first_entered = asyncio.Event()
    finish_first = asyncio.Event()
    order = []

    async def first_waiter():
        first_waiting.set()
        async with store.serialized("session"):
            order.append("first")
            first_entered.set()
            await finish_first.wait()

    async def late_waiter():
        async with store.serialized("session"):
            order.append("late")

    tasks = []
    try:
        async with store.serialized("session"):
            tasks.append(asyncio.create_task(first_waiter()))
            await asyncio.wait_for(first_waiting.wait(), 5)
        # The old holder has exited, but a queued caller still owns a reference.
        # A new arrival must not obtain a newly allocated, independent lock.
        tasks.append(asyncio.create_task(late_waiter()))
        await asyncio.wait_for(first_entered.wait(), 5)
        await asyncio.sleep(0)
        assert order == ["first"]
        assert len(store._locks) == 1
    finally:
        finish_first.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert order == ["first", "late"]
    gc.collect()
    assert not store._locks


async def test_cancelled_waiter_does_not_split_active_lock():
    store = EngineStore(":memory:")
    entered = []

    async def contender(name):
        async with store.serialized("session"):
            entered.append(name)

    async with store.serialized("session"):
        cancelled = asyncio.create_task(contender("cancelled"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        survivor = asyncio.create_task(contender("survivor"))
        await asyncio.sleep(0)
        assert entered == []
        assert len(store._locks) == 1
    await asyncio.wait_for(survivor, 5)
    assert entered == ["survivor"]


def test_completed_and_failed_merchant_operations_retire_keys():
    store = EngineStore(":memory:")
    for index in range(1000):
        with store.merchant_operation(f"change-{index}"):
            assert store._merchant_operations == {f"change-{index}"}
    with pytest.raises(RuntimeError, match="failed"):
        with store.merchant_operation("failed-change"):
            raise RuntimeError("failed")
    assert not store._merchant_operations
    with store.merchant_operation("failed-change"):
        pass


def test_merchant_retirement_preserves_cross_thread_exclusion():
    store = EngineStore(":memory:")
    entered = threading.Event()
    finish = threading.Event()

    def owner():
        with store.merchant_operation("change"):
            entered.set()
            assert finish.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner)
        try:
            assert entered.wait(5)
            with pytest.raises(MerchantOperationBusy):
                with store.merchant_operation("change"):
                    pytest.fail("two owners entered the same merchant operation")
            with store.merchant_operation("other-change"):
                assert store._merchant_operations == {"change", "other-change"}
        finally:
            finish.set()
        future.result(timeout=5)
    assert not store._merchant_operations
    with store.merchant_operation("change"):
        pass


def test_cross_worker_refusal_retires_local_merchant_key(tmp_path):
    path = str(tmp_path / "store.db")
    first = EngineStore(path)
    second = EngineStore(path)
    with first.merchant_operation("change"):
        with pytest.raises(MerchantOperationBusy):
            with second.merchant_operation("change"):
                pytest.fail("OS-held ownership was ignored")
        assert not second._merchant_operations
    with second.merchant_operation("change"):
        pass
    assert not first._merchant_operations
    assert not second._merchant_operations
