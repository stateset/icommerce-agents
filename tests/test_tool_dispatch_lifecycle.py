import asyncio
from types import SimpleNamespace

import pytest
from commerce_common.testing import FakeStream, tool_use_message
from merchant_agent.types import MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent

from engine_backend import SKILLS_DIR
from engine_backend.agent_config import merchant_agent_config, shopping_agent_config
from engine_backend.merchant import EngineMerchant
from engine_backend.storefront import EngineStorefront


@pytest.mark.parametrize("role", ["shopping", "merchant"])
async def test_interrupted_model_stream_does_not_start_detached_tool(
    store, kernel, monkeypatch, role
):
    stopped = asyncio.Event()
    called = asyncio.Event()
    tool = "search_products" if role == "shopping" else "get_business_snapshot"
    arguments = {"query": "tent"} if role == "shopping" else {}

    class InterruptedStream(FakeStream):
        def __aiter__(self):
            async def events():
                async for event in super(InterruptedStream, self).__aiter__():
                    yield event
                    if event.type == "content_block_stop":
                        stopped.set()
                        # A disconnected stream after a complete tool block is the
                        # path where eager tasks would have escaped the model round.
                        await asyncio.Event().wait()

            return events()

    client = SimpleNamespace(
        messages=SimpleNamespace(
            stream=lambda **kwargs: InterruptedStream(tool_use_message(tool, arguments))
        )
    )
    if role == "shopping":
        backend = EngineStorefront(store)
        customer = store.commerce.customers.get_by_email("rowan@example.invalid")
        session = ShoppingSessionContext(session_id="session", user_id=customer.id)
        store.bind("session", customer.id, "customer")
        state = ShoppingSessionState()
        agent = ShoppingAgent(
            backend=backend,
            config=shopping_agent_config(),
            skills_dir=SKILLS_DIR(role),
            client=client,
        )
    else:
        backend = EngineMerchant(store, kernel)
        session = MerchantSessionContext(
            session_id="session", merchant_id=store.store_id, operator="operator"
        )
        store.bind("session", "operator", "operator")
        state = MerchantSessionState()
        agent = MerchantAgent(
            backend=backend,
            config=merchant_agent_config(),
            skills_dir=SKILLS_DIR(role),
            client=client,
        )
    original = getattr(backend, tool)

    async def observed(*args, **kwargs):
        called.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(backend, tool, observed)

    async def consume():
        async for _ in agent.stream_turn(
            [{"role": "user", "content": "show current state"}], session, state
        ):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(stopped.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(called.wait(), 0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not called.is_set()
