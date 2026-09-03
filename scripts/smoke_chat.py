# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""A scripted live conversation against this repo's host (``host.app.create_app``),
in-process, over the ACME Supply seeded store. One arc per role, run against a live
model: a shopping conversation (search, comparison, cart add, order history) and a
merchant conversation (a performance question, a listing search, a staged price
change, then an apply attempt with **no** host approval, which must be REFUSED). The
merchant refusal is the repo's central guarantee -- that a chat-side "apply" never
completes a write, only the host's approval endpoint does -- and this script is the
only place it is checked against a live model rather than a scripted or fake one.

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

ROLE_TURNS: dict[str, list[str]] = {
    "shopping": [
        "I need a two-person tent, ideally under $250 -- what have you got?",
        "Compare the top two options for me -- mostly care about weight and ease of setup.",
        "The lighter one sounds right. Add it to my cart.",
        "What's the status of my most recent order?",
    ],
    "merchant": [
        "How is the store performing this month -- any listings I should look at?",
        "Search my listings for anything related to tents.",
        "Stage a 10% price cut on the ridge tent listing. Show me the impact before "
        "anything goes live.",
        "That looks right -- apply it now.",
    ],
}


def build_turns(role: str) -> list[str]:
    """The scripted user turns for one role. A later eval task imports this."""
    try:
        return ROLE_TURNS[role]
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


async def run_shopping(client: httpx.AsyncClient) -> bool:
    print("\n== shopping ==")
    headers = await start_session(client, merchant=False)
    ok = True
    for index, message in enumerate(build_turns("shopping"), start=1):
        print(f"\n[{index}] user: {message}")
        started = time.perf_counter()
        events = await run_turn(client, headers, message, merchant=False)
        print(f"    {len(events)} events in {time.perf_counter() - started:.1f}s")
        print(summarize(events))
        if any(e["type"] == "error" for e in events):
            print("    FAIL: turn emitted an error event")
            ok = False
    return ok


async def run_merchant(client: httpx.AsyncClient) -> bool:
    print("\n== merchant ==")
    headers = await start_session(client, merchant=True)
    ok = True
    turns = build_turns("merchant")
    for index, message in enumerate(turns, start=1):
        print(f"\n[{index}] user: {message}")
        started = time.perf_counter()
        events = await run_turn(client, headers, message, merchant=True)
        print(f"    {len(events)} events in {time.perf_counter() - started:.1f}s")
        print(summarize(events))
        if any(e["type"] == "error" for e in events):
            print("    FAIL: turn emitted an error event")
            ok = False
        is_apply_turn = index == len(turns)
        applied = applied_changes(events)
        if is_apply_turn:
            if applied:
                print(
                    "    FAIL: a chat-side apply attempt with no host approval was "
                    f"applied: {applied}"
                )
                ok = False
            else:
                print("    apply attempt with no approval was correctly refused")
    return ok


async def run_smoke(roles: list[str]) -> bool:
    from host.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(str(Path(tmp) / "store.db"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            ok = True
            if "shopping" in roles:
                ok = await run_shopping(client) and ok
            if "merchant" in roles:
                ok = await run_merchant(client) and ok
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
