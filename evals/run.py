"""Drives ``evals/cases.py`` against a live model and grades the transcripts.

    python -m evals.run

No ``ANTHROPIC_API_KEY`` exists on this machine or in CI, so this module has never been
run end to end here -- see ``evals/README.md``. With no key, ``main`` prints that and
exits 0, the same contract as ``scripts/smoke_chat.py``.

``run()`` takes its ``client`` as an argument so tests exercise it with
``commerce_common.testing.FakeClient`` instead of a real ``AsyncAnthropic``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce_common.streaming import AgentEvent  # noqa: E402
from merchant_agent.types import MerchantSessionContext, MerchantSessionState  # noqa: E402
from merchant_agent_runtime import MerchantAgent  # noqa: E402
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState  # noqa: E402
from shopping_agent_runtime import ShoppingAgent  # noqa: E402

from engine_backend import SKILLS_DIR  # noqa: E402
from engine_backend.kernel import KernelClient  # noqa: E402
from engine_backend.merchant import EngineMerchant  # noqa: E402
from engine_backend.seed import seed_store  # noqa: E402
from engine_backend.store import EngineStore  # noqa: E402
from engine_backend.storefront import EngineStorefront  # noqa: E402

from .cases import CASES  # noqa: E402
from .graders import EvalCase, EvalResult, grade  # noqa: E402

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_CUSTOMER_EMAIL = "rowan@example.invalid"
_OPERATOR_ID = "user:acme-operator"


def _new_deployment(db_path: str) -> tuple[EngineStorefront, EngineMerchant, EngineStore]:
    store = EngineStore(db_path)
    seed_store(store.commerce)
    storefront = EngineStorefront(store)
    kernel = KernelClient(
        store, CONFIG_DIR / "kernel-policy.json", CONFIG_DIR / "kernel-principal.json"
    )
    merchant = EngineMerchant(store, kernel)
    return storefront, merchant, store


async def _run_case(case: EvalCase, client: Any, db_path: str) -> EvalResult:
    storefront, merchant, store = _new_deployment(db_path)

    if case.role == "shopping":
        customer = store.commerce.customers.get_by_email(_CUSTOMER_EMAIL)
        agent = ShoppingAgent(backend=storefront, skills_dir=SKILLS_DIR("shopping"), client=client)
        session = ShoppingSessionContext(session_id=f"eval-{case.id}", user_id=customer.id)
        state = ShoppingSessionState()
    elif case.role == "merchant":
        agent = MerchantAgent(backend=merchant, skills_dir=SKILLS_DIR("merchant"), client=client)
        session = MerchantSessionContext(
            session_id=f"eval-{case.id}", merchant_id=store.store_id, operator=_OPERATOR_ID
        )
        state = MerchantSessionState()
    else:
        raise ValueError(f"unknown role {case.role!r}")

    messages: list[dict[str, Any]] = []

    # The case's scripted lead-in, if it has one: driven through the same agent, session
    # and store, but not graded. This is what puts a cart on the store before the
    # checkout case's own turn -- each case gets a fresh store, so without it that turn
    # would have nothing to check out and would fail for a reason unrelated to its rule.
    for turn in case.lead_in:
        messages.append({"role": "user", "content": turn})
        async for _event in agent.stream_turn(messages, session, state):
            pass

    messages.append({"role": "user", "content": case.prompt})
    transcript: list[AgentEvent] = [
        event async for event in agent.stream_turn(messages, session, state)
    ]
    return grade(case, transcript)


async def _run_all(cases: list[EvalCase], client: Any) -> list[EvalResult]:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, case in enumerate(cases):
            db_path = str(Path(tmp) / f"eval-{index}.db")
            results.append(await _run_case(case, client, db_path))
    return results


def run(cases: list[EvalCase], client: Any) -> list[EvalResult]:
    """Run every case in ``cases`` against ``client`` and grade the transcripts. Each
    case gets its own freshly seeded store, so cases never interact."""
    return asyncio.run(_run_all(cases, client))


def main(argv: list[str] | None = None) -> int:
    del argv
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "No ANTHROPIC_API_KEY set -- skipping the eval suite. This suite has never "
            "been run against a live model in this environment; set ANTHROPIC_API_KEY "
            "to exercise it."
        )
        return 0

    from host.anthropic_client import build_anthropic_client

    client = build_anthropic_client()
    results = run(CASES, client)
    failures = [r for r in results if not r.passed]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.case_id}: {result.reason}")
    print(f"\n{len(results) - len(failures)}/{len(results)} passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
