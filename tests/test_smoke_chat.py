import pytest
from commerce_common.testing import FakeClient, text_message
from merchant_agent.types import MerchantSessionContext, MerchantSessionState
from merchant_agent_runtime import MerchantAgent

from engine_backend import SKILLS_DIR
from engine_backend.merchant import EngineMerchant
from scripts import smoke_chat


def test_build_turns_shopping_is_non_empty():
    turns = smoke_chat.build_turns("shopping")
    assert isinstance(turns, list)
    assert len(turns) >= 4
    assert all(isinstance(t, str) and t for t in turns)


def test_build_turns_merchant_is_non_empty():
    turns = smoke_chat.build_turns("merchant")
    assert isinstance(turns, list)
    assert len(turns) >= 4
    assert all(isinstance(t, str) and t for t in turns)


def test_build_turns_unknown_role_raises():
    with pytest.raises(ValueError):
        smoke_chat.build_turns("nope")


def test_main_with_no_api_key_exits_0_with_a_clear_message(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exit_code = smoke_chat.main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ANTHROPIC_API_KEY" in captured.out


# -- check_turn: the refusal check must not pass vacuously ----------------------------


def _tool_call_event(tool: str) -> dict:
    return {"type": "tool_call", "data": {"tool": tool, "id": "tu-1", "input": {}}}


def _applied_change_event() -> dict:
    return {"type": "change_update", "data": {"change": {"status": "applied"}}}


APPLY_TURN = smoke_chat.ROLE_TURNS["merchant"][-1]


def test_check_turn_fails_when_the_expected_tool_is_never_called():
    # The model just says "I can't do that" and never calls apply_change -- no
    # change_update at all. This must be a FAILURE, not a silent "correctly refused".
    events: list[dict] = []
    failures = smoke_chat.check_turn(events, APPLY_TURN)
    assert failures
    assert any("apply_change" in failure for failure in failures)


def test_check_turn_fails_when_the_tool_was_called_and_applied():
    events = [_tool_call_event("apply_change"), _applied_change_event()]
    failures = smoke_chat.check_turn(events, APPLY_TURN)
    assert failures
    assert any("applied" in failure for failure in failures)


def test_check_turn_passes_when_the_tool_was_called_and_refused(capsys):
    events = [_tool_call_event("apply_change")]
    failures = smoke_chat.check_turn(events, APPLY_TURN)
    assert failures == []
    assert "correctly refused" in capsys.readouterr().out


def test_check_turn_reports_missing_tool_and_unwanted_apply_distinctly():
    # A missing expected tool and an unauthorized apply are different failures, and
    # the message must let a reader tell them apart.
    missing = smoke_chat.check_turn([], {"expect_tools": {"search_products"}})
    assert any("search_products" in f and "called" in f for f in missing)


async def test_a_no_tool_reply_is_caught_end_to_end_with_a_fake_client(store, kernel):
    """The regression this guards against: a live model that answers the apply
    request in prose without ever calling ``apply_change``. Wired through the real
    ``MerchantAgent`` and ``EngineMerchant`` backend with a scripted (no-API-key)
    client, this must still fail check_turn -- not read as a correct refusal."""
    backend = EngineMerchant(store, kernel)
    agent = MerchantAgent(
        backend=backend,
        skills_dir=SKILLS_DIR("merchant"),
        client=FakeClient([text_message("I can't apply that without your approval.")]),
    )
    session = MerchantSessionContext(
        session_id="m-1", merchant_id=store.store_id, operator="user:acme-operator"
    )
    messages = [{"role": "user", "content": APPLY_TURN["message"]}]
    events = [
        {"type": event.type, "data": event.data}
        async for event in agent.stream_turn(messages, session, MerchantSessionState())
    ]

    failures = smoke_chat.check_turn(events, APPLY_TURN)

    assert failures, "a turn that never attempted apply_change must fail, not pass vacuously"
    assert any("apply_change" in failure for failure in failures)
