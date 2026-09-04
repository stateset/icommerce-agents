from commerce_common.streaming import AgentEvent

from host.response_policy import (
    TurnResponsePolicy,
    replace_latest_assistant_text,
    safe_response_text,
)


def _result(tool: str, status: str = "ok") -> AgentEvent:
    return AgentEvent.tool_result(tool, "tool-1", "done", status=status)


def test_staged_change_cannot_be_described_as_applied():
    text, changed = safe_response_text(
        "merchant",
        "lower both prices",
        [_result("stage_price_update")],
        "I applied it to both variants. Approve it when ready.",
    )
    assert changed
    assert "staged successfully" in text
    assert "has not been applied" in text


def test_real_successful_apply_is_not_rewritten():
    original = "I applied the approved change."
    text, changed = safe_response_text(
        "merchant",
        "apply it",
        [_result("stage_price_update"), _result("apply_change")],
        original,
    )
    assert not changed
    assert text == original


def test_medical_request_gets_a_qualified_referral_when_model_omits_it():
    text, changed = safe_response_text(
        "shopping",
        "Is this safe with my allergy?",
        [],
        "The Trail Meal is marked gluten-free. Check the manufacturer documentation.",
    )
    assert changed
    assert "qualified clinician" in text
    assert "pharmacist" in text


def test_existing_qualified_referral_is_unchanged():
    original = "Consider this product, and ask your allergist before using it."
    text, changed = safe_response_text("shopping", "Could this affect my allergy?", [], original)
    assert not changed
    assert text == original


def test_ordinary_product_condition_question_does_not_trigger_medical_policy():
    original = "The returned item is in good condition."
    text, changed = safe_response_text(
        "shopping", "What condition is the returned item in?", [], original
    )
    assert not changed
    assert text == original


def test_turn_policy_streams_non_text_and_emits_checked_text_before_completion():
    policy = TurnResponsePolicy("merchant", "stage it")
    assert policy.accept(AgentEvent.text_delta("I applied it.")) == []
    tool = _result("stage_price_update")
    assert policy.accept(tool) == [tool]
    complete = AgentEvent.turn_complete(None, {}, 1, 0)
    emitted = policy.accept(complete)
    assert emitted[-1] == complete
    assert emitted[0].type == "text_delta"
    assert "staged successfully" in emitted[0].data["text"]


def test_rewritten_text_replaces_model_history_for_future_turns():
    messages = [
        {"role": "user", "content": "stage it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I applied it."},
                {"type": "text", "text": " It is live."},
            ],
        },
    ]
    replace_latest_assistant_text(messages, "It is staged only.")
    assert messages[-1]["content"] == [{"type": "text", "text": "It is staged only."}]
