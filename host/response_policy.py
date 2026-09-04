"""Deterministic last-mile policy for model-authored host responses.

The agent gates protect state.  This module protects the operator-facing description
of that state for the two failure modes found by the live evaluation suite.  Tool and
UI events continue to stream immediately; text is held until the turn completes so an
unsafe claim is never partially rendered before it can be checked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from commerce_common.streaming import AgentEvent

Role = Literal["shopping", "merchant"]

_MEDICAL_REQUEST = re.compile(
    r"\b(allerg(?:y|ic|ies)|injur(?:y|ed|ies)|medical|clinician|doctor|pharmacist)\b|"
    r"\bhealth condition\b|\bsafe (?:for|with) (?:me|my)\b",
    re.IGNORECASE,
)
_QUALIFIED_REFERRAL = re.compile(
    r"\b(clinician|doctor|allergist|pharmacist|medical professional|healthcare "
    r"professional)\b",
    re.IGNORECASE,
)
_FALSE_APPLY_CLAIM = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:applied|completed|published|activated)\b|"
    r"\b(?:change|price|listing|promotion|campaign)\s+(?:has\s+been|is|is\s+now)\s+"
    r"(?:applied|completed|live|published|active)\b",
    re.IGNORECASE,
)

_STAGED_FALLBACK = (
    "The requested change was staged successfully as a proposal. It has not been "
    "applied and is not live. Review the structured change preview and approve its "
    "proposal digest before applying it."
)
_MEDICAL_REFERRAL = (
    "For the medical, injury, or allergy aspect, consult a qualified clinician, "
    "doctor, allergist, or pharmacist before purchasing or using the product."
)


def safe_response_text(
    role: Role, user_message: str, events: Iterable[AgentEvent], text: str
) -> tuple[str, bool]:
    """Return policy-compliant display text and whether it changed.

    This is intentionally narrow.  It does not claim to validate arbitrary model
    output; it deterministically closes the two concrete behaviors exercised by the
    repository's live evals.
    """
    observed = list(events)
    if role == "merchant":
        staged = any(
            event.type == "tool_result"
            and str(event.data.get("tool", "")).startswith("stage_")
            and event.data.get("status") == "ok"
            for event in observed
        )
        applied = any(
            event.type == "tool_result"
            and event.data.get("tool") == "apply_change"
            and event.data.get("status") == "ok"
            for event in observed
        )
        if staged and not applied and _FALSE_APPLY_CLAIM.search(text):
            return _STAGED_FALLBACK, True

    if (
        role == "shopping"
        and _MEDICAL_REQUEST.search(user_message)
        and not _QUALIFIED_REFERRAL.search(text)
    ):
        separator = " " if text.strip() else ""
        return text.rstrip() + separator + _MEDICAL_REFERRAL, True

    return text, False


class TurnResponsePolicy:
    """Buffer only text events, then emit one checked text event before completion."""

    def __init__(self, role: Role, user_message: str) -> None:
        self.role = role
        self.user_message = user_message
        self.events: list[AgentEvent] = []
        self.text_parts: list[str] = []
        self.final_text = ""
        self.rewritten = False
        self._flushed = False

    def accept(self, event: AgentEvent) -> list[AgentEvent]:
        self.events.append(event)
        if event.type == "text_delta":
            self.text_parts.append(str(event.data.get("text", "")))
            return []
        if event.type == "turn_complete":
            return [*self.flush(), event]
        return [event]

    def flush(self) -> list[AgentEvent]:
        if self._flushed:
            return []
        self._flushed = True
        original = "".join(self.text_parts)
        self.final_text, self.rewritten = safe_response_text(
            self.role, self.user_message, self.events, original
        )
        return [AgentEvent.text_delta(self.final_text)] if self.final_text else []


def replace_latest_assistant_text(messages: list[dict[str, Any]], text: str) -> None:
    """Keep future model context aligned with the checked text shown to the person."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = text
            return
        if not isinstance(content, list):
            continue
        indexes = [
            index
            for index, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not indexes:
            continue
        content[indexes[0]]["text"] = text
        for index in reversed(indexes[1:]):
            del content[index]
        return


__all__ = [
    "TurnResponsePolicy",
    "replace_latest_assistant_text",
    "safe_response_text",
]
