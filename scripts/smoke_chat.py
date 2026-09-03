"""A scripted live conversation against this repo's host (``host.app.create_app``),
in-process, over the ACME Supply seeded store. One arc per role, run against a live
model: a shopping conversation (search, comparison, cart add, order history) and a
merchant conversation (a performance question, a listing search, a staged price
change, then an apply attempt with **no** host approval, which must be REFUSED). The
merchant refusal is the repo's central guarantee -- that a chat-side "apply" never
completes a write, only the host's approval endpoint does -- and this script is the
only place it is checked against a live model rather than a scripted or fake one.

Each turn carries the tool(s) it expects to see called (``expect_tools``), the way
upstream's ``vendor/commerce-agents/scripts/smoke_chat.py`` does: a turn that never
calls its expected tool is a FAILURE, not quiet success -- in particular, the final
merchant turn expects ``apply_change`` to be called *and* checks that no
``change_update`` reports the change as ``applied``. A model that never attempts the
tool at all is caught by the first check rather than being mistaken for a correct
refusal.

    python scripts/smoke_chat.py [--role shopping|merchant]
    python scripts/smoke_chat.py            # runs both roles

This has never been run against a live model: it needs ``ANTHROPIC_API_KEY``, which
is not set in this environment or in CI, and each run against a real key costs a few
cents. Without a key, ``main`` prints a message and exits 0 -- this script's own tests
exercise only that path, never a live model turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SESSION_HEADER = "X-Session-Id"

# One arc per role. ``expect_tools`` names the tool(s) a turn must be seen calling;
# ``forbid_applied_change`` marks a turn whose ``apply_change`` call must not result in
# a ``change_update`` reporting the change as applied -- the host, not the chat, owns
# approval.
ROLE_TURNS: dict[str, list[dict[str, Any]]] = {
    "shopping": [
        {
            "message": "I need a two-person tent, ideally under $250 -- what have you got?",
            "expect_tools": {"search_products"},
        },
        {
            "message": (
                "Compare the top two options for me -- mostly care about weight and ease of setup."
            ),
            "expect_tools": set(),
        },
        {
            "message": "The lighter one sounds right. Add it to my cart.",
            "expect_tools": {"add_to_cart"},
        },
        {
            "message": "What's the status of my most recent order?",
            "expect_tools": {"get_order_status", "get_orders"},
        },
    ],
    "merchant": [
        {
            "message": "How is the store performing this month -- any listings I should look at?",
            "expect_tools": {"get_business_snapshot"},
        },
        {
            "message": "Search my listings for anything related to tents.",
            "expect_tools": {"search_listings"},
        },
        {
            "message": (
                "Stage a 10% price cut on the ridge tent listing. Show me the impact "
                "before anything goes live."
            ),
            "expect_tools": {"stage_price_update"},
        },
        {
            "message": "That looks right -- apply it now.",
            "expect_tools": {"apply_change"},
            "forbid_applied_change": True,
        },
    ],
}


def build_turns(role: str) -> list[str]:
    """The scripted user turns for one role, without their per-turn expectations.

    `tests/test_smoke_chat.py` is the only caller. The eval suite was once meant to
    reuse this for its scripted lead-ins and does not: a lead-in is one deliberate turn
    per case (`EvalCase.lead_in`), not a whole role arc, and driving four vague turns
    through a live model to set up one graded turn would add cost and flake without
    making the case sharper."""
    try:
        return [turn["message"] for turn in ROLE_TURNS[role]]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLE_TURNS)}") from None


async def start_session(client: httpx.AsyncClient, *, merchant: bool) -> dict[str, str]:
    path = "/merchant/session" if merchant else "/shopping/session"
    response = await client.post(path)
    response.raise_for_status()
    return {SESSION_HEADER: response.json()["session_id"]}


async def run_turn(
    client: httpx.AsyncClient, headers: dict[str, str], message: str, *, merchant: bool
) -> list[dict[str, Any]]:
    path = "/merchant/chat" if merchant else "/shopping/chat"
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST", path, json={"message": message}, headers=headers, timeout=180.0
    ) as response:
        response.raise_for_status()
        current_event: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ").strip()
            elif line.startswith("data: ") and current_event:
                events.append(
                    {"type": current_event, "data": json.loads(line.removeprefix("data: "))}
                )
    return events


def summarize(events: list[dict[str, Any]]) -> str:
    text = "".join(e["data"].get("text", "") for e in events if e["type"] == "text_delta")
    tools = [e["data"].get("tool") for e in events if e["type"] == "tool_call"]
    return f"    tools: {tools or '-'}\n    text: {len(text)} chars"


def applied_changes(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes = ((e["data"].get("change") or {}) for e in events if e["type"] == "change_update")
    return [change for change in changes if change.get("status") == "applied"]


def check_turn(events: list[dict[str, Any]], turn: dict[str, Any]) -> list[str]:
    """Behavior checks for one turn's events, in upstream's shape: an expected tool
    that was never called is a failure in its own right, distinct from (and checked
    before) whether an ``apply_change`` call was, correctly, never applied. A turn
    with no ``expect_tools`` is a liveness check only -- it still fails on an error
    event."""
    failures: list[str] = []
    seen_tools = {e["data"].get("tool") for e in events if e["type"] == "tool_call"}
    expected = turn.get("expect_tools") or set()
    if expected and not (expected & seen_tools):
        failures.append(
            f"expected one of {sorted(expected)} to be called, saw {sorted(seen_tools) or 'none'}"
        )
    if any(e["type"] == "error" for e in events):
        failures.append("turn emitted an error event")
    if turn.get("forbid_applied_change"):
        attempted = "apply_change" in seen_tools
        applied = applied_changes(events)
        if attempted and applied:
            failures.append(
                "a chat-side apply attempt with no host approval was applied: "
                f"{applied} -- the host's approval gate did not hold"
            )
        elif attempted and not applied:
            print("    apply attempt with no approval was correctly refused")
    return failures


async def run_role(client: httpx.AsyncClient, role: str, *, merchant: bool) -> bool:
    print(f"\n== {role} ==")
    headers = await start_session(client, merchant=merchant)
    ok = True
    turns = ROLE_TURNS[role]
    for index, turn in enumerate(turns, start=1):
        print(f"\n[{index}] user: {turn['message']}")
        started = time.perf_counter()
        events = await run_turn(client, headers, turn["message"], merchant=merchant)
        print(f"    {len(events)} events in {time.perf_counter() - started:.1f}s")
        print(summarize(events))
        failures = check_turn(events, turn)
        for failure in failures:
            print(f"    FAIL: {failure}")
        ok = ok and not failures
    return ok


async def run_smoke(roles: list[str]) -> bool:
    from host.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(str(Path(tmp) / "store.db"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            ok = True
            if "shopping" in roles:
                ok = await run_role(client, "shopping", merchant=False) and ok
            if "merchant" in roles:
                ok = await run_role(client, "merchant", merchant=True) and ok
            return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--role",
        choices=sorted(ROLE_TURNS),
        help="which role to run; runs both when omitted",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "No ANTHROPIC_API_KEY set -- skipping the live smoke conversation. "
            "This script has never been run against a live model in this "
            "environment; set ANTHROPIC_API_KEY to exercise it."
        )
        return 0

    roles = [args.role] if args.role else sorted(ROLE_TURNS)
    ok = asyncio.run(run_smoke(roles))
    print("\nSMOKE", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
