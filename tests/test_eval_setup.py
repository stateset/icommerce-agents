import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from commerce_common.streaming import AgentEvent

from engine_backend.storefront import EngineStorefront
from evals import run as runner
from evals.graders import EvalCase, Grader, Verdict


def case(*, role="shopping", requires_cart=True, lead_in=("setup",)):
    return EvalCase(
        id="setup-integrity",
        role=role,
        prompt="graded prompt",
        lead_in=lead_in,
        requires_cart=requires_cart,
        grader=Grader(check=lambda _: Verdict(True, "behavioral check passed")),
        why="a passing grader cannot override failed prerequisites",
    )


def agent(monkeypatch, scripts, *, populate=False):
    requests, closed = [], []

    class ScriptedAgent:
        def __init__(self, *, backend, **kwargs):
            self.backend = backend

        async def stream_turn(self, messages, session, state):
            index = len(requests)
            requests.append(messages[-1]["content"])
            try:
                if populate and index == 0:
                    await self.backend.add_to_cart(session, "TENT-RIDGE-GRN", 1)
                for event in scripts[index]:
                    yield event
            finally:
                closed.append(index)

    monkeypatch.setattr(runner, "ShoppingAgent", ScriptedAgent)
    monkeypatch.setattr(runner, "MerchantAgent", ScriptedAgent)
    return requests, closed


@pytest.fixture
def deployment(monkeypatch):
    storefront = SimpleNamespace(get_cart=AsyncMock(return_value=SimpleNamespace(items=[])))
    store = SimpleNamespace(
        store_id="store:test",
        commerce=SimpleNamespace(
            customers=SimpleNamespace(get_by_email=lambda _: SimpleNamespace(id="customer"))
        ),
        bind=Mock(),
    )
    monkeypatch.setattr(runner, "_new_deployment", lambda _: (storefront, object(), store))
    return storefront


@pytest.mark.parametrize("role", ["shopping", "merchant"])
@pytest.mark.parametrize(
    ("events", "reason"),
    [
        ([], "no assistant response"),
        ([AgentEvent.text_delta(" \n")], "no assistant response"),
        ([AgentEvent(type="error", data={"message": "private provider details"})], "agent error"),
        (
            [AgentEvent.text_delta("Done."), AgentEvent(type="error", data={"message": "failed"})],
            "agent error",
        ),
    ],
)
async def test_setup_failure_never_reaches_graded_turn(
    monkeypatch, deployment, role, events, reason
):
    requests, closed = agent(monkeypatch, [events])
    result = await runner._run_case(case(role=role, requires_cart=False), object(), "unused")
    assert not result.passed
    assert reason in result.reason
    assert "private provider details" not in result.reason
    assert requests == ["setup"]
    assert closed == [0]
    deployment.get_cart.assert_not_awaited()


async def test_claim_of_success_cannot_replace_actual_cart_state(monkeypatch, deployment):
    requests, closed = agent(monkeypatch, [[AgentEvent.text_delta("I added the tent.")]])
    result = await runner._run_case(case(), object(), "unused")
    assert not result.passed
    assert "engine-backed cart" in result.reason
    assert requests == ["setup"]
    assert closed == [0]
    deployment.get_cart.assert_awaited_once()


async def test_later_setup_failure_stops_remaining_setup_and_grading(monkeypatch, deployment):
    requests, closed = agent(monkeypatch, [[AgentEvent.text_delta("Done.")], []])
    result = await runner._run_case(case(lead_in=("first", "second", "third")), object(), "unused")
    assert not result.passed
    assert "setup turn 2" in result.reason
    assert requests == ["first", "second"]
    assert closed == [0, 1]


async def test_populated_real_cart_allows_grading(monkeypatch, store):
    storefront = EngineStorefront(store)
    monkeypatch.setattr(runner, "_new_deployment", lambda _: (storefront, object(), store))
    requests, closed = agent(
        monkeypatch,
        [[AgentEvent.text_delta("Added the tent.")], [AgentEvent.text_delta("Ready for review.")]],
        populate=True,
    )
    async with asyncio.timeout(30):
        result = await runner._run_case(case(), object(), "unused")
    assert result.passed
    assert requests == ["setup", "graded prompt"]
    assert closed == [0, 1]
    cart_id = storefront.session_cart_id("eval-setup-integrity")
    assert store.commerce.carts.get_items(cart_id)[0].sku == "TENT-RIDGE-GRN"


async def test_cases_without_cart_prerequisite_do_not_read_cart(monkeypatch, deployment):
    requests, closed = agent(monkeypatch, [[AgentEvent.text_delta("Answer.")]])
    result = await runner._run_case(case(requires_cart=False, lead_in=()), object(), "unused")
    assert result.passed
    assert requests == ["graded prompt"]
    assert closed == [0]
    deployment.get_cart.assert_not_awaited()


async def test_invalid_cart_prerequisite_is_rejected_before_deployment(monkeypatch):
    build = Mock()
    monkeypatch.setattr(runner, "_new_deployment", build)
    with pytest.raises(ValueError, match="only valid for shopping"):
        await runner._run_case(case(role="merchant"), object(), "unused")
    build.assert_not_called()
