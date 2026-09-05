"""Drives ``evals/cases.py`` against a live model and grades the transcripts.

    python -m evals.run

CI has no ``ANTHROPIC_API_KEY`` and does not run this end to end; dated manual results
are recorded in ``evals/README.md``. With no key, ``main`` prints that it skipped and
exits 0, the same contract as ``scripts/smoke_chat.py``.

``run()`` takes its ``client`` as an argument so tests exercise it with
``commerce_common.testing.FakeClient`` instead of a real ``AsyncAnthropic``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from contextlib import aclosing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commerce_common.streaming import AgentEvent  # noqa: E402
from merchant_agent.types import MerchantSessionContext, MerchantSessionState  # noqa: E402
from merchant_agent_runtime import MerchantAgent  # noqa: E402
from shopping_agent.types import ShoppingSessionContext, ShoppingSessionState  # noqa: E402
from shopping_agent_runtime import ShoppingAgent  # noqa: E402

from engine_backend import SKILLS_DIR  # noqa: E402
from engine_backend.agent_config import (  # noqa: E402
    merchant_agent_config,
    shopping_agent_config,
)
from engine_backend.kernel import KernelClient  # noqa: E402
from engine_backend.merchant import EngineMerchant  # noqa: E402
from engine_backend.seed import seed_store  # noqa: E402
from engine_backend.store import EngineStore  # noqa: E402
from engine_backend.storefront import EngineStorefront  # noqa: E402
from scripts.live_eval_check import serialize_report  # noqa: E402

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
    if case.requires_cart and case.role != "shopping":
        raise ValueError("requires_cart is only valid for shopping cases")
    storefront, merchant, store = _new_deployment(db_path)

    if case.role == "shopping":
        customer = store.commerce.customers.get_by_email(_CUSTOMER_EMAIL)
        agent = ShoppingAgent(
            backend=storefront,
            skills_dir=SKILLS_DIR("shopping"),
            config=shopping_agent_config(),
            client=client,
        )
        session = ShoppingSessionContext(session_id=f"eval-{case.id}", user_id=customer.id)
        store.bind(session.session_id, customer.id, "customer")
        state = ShoppingSessionState()
    elif case.role == "merchant":
        agent = MerchantAgent(
            backend=merchant,
            skills_dir=SKILLS_DIR("merchant"),
            config=merchant_agent_config(),
            client=client,
        )
        session = MerchantSessionContext(
            session_id=f"eval-{case.id}", merchant_id=store.store_id, operator=_OPERATOR_ID
        )
        store.bind(session.session_id, _OPERATOR_ID, "operator")
        state = MerchantSessionState()
    else:
        raise ValueError(f"unknown role {case.role!r}")

    messages: list[dict[str, Any]] = []

    # The case's scripted lead-in, if it has one: driven through the same agent, session
    # and store, but not graded. This is what puts a cart on the store before the
    # checkout case's own turn -- each case gets a fresh store, so without it that turn
    # would have nothing to check out and would fail for a reason unrelated to its rule.
    for index, turn in enumerate(case.lead_in, start=1):
        messages.append({"role": "user", "content": turn})
        responded = False
        async with aclosing(agent.stream_turn(messages, session, state)) as events:
            async for event in events:
                if event.type == "error":
                    return EvalResult(case.id, False, f"setup turn {index} emitted an agent error")
                if event.type == "text_delta" and event.data.get("text", "").strip():
                    responded = True
        if not responded:
            return EvalResult(case.id, False, f"setup turn {index} produced no assistant response")

    if case.requires_cart:
        cart = await storefront.get_cart(session)
        if not cart.items:
            return EvalResult(case.id, False, "setup did not populate the engine-backed cart")

    messages.append({"role": "user", "content": case.prompt})
    async with aclosing(agent.stream_turn(messages, session, state)) as events:
        transcript: list[AgentEvent] = [event async for event in events]
    return grade(case, transcript)


async def _iter_results(cases: list[EvalCase], client: Any, *, case_timeout_seconds: int = 120):
    with tempfile.TemporaryDirectory() as tmp:
        for index, case in enumerate(cases):
            db_path = str(Path(tmp) / f"eval-{index}.db")
            async with asyncio.timeout(case_timeout_seconds):
                result = await _run_case(case, client, db_path)
            yield result


async def _run_all(cases: list[EvalCase], client: Any) -> list[EvalResult]:
    return [result async for result in _iter_results(cases, client)]


def _checkpoint_report(path: Path | None, report: dict[str, Any]) -> None:
    """Replace a report atomically; a failed write preserves the last checkpoint."""
    if path is None:
        return
    encoded = serialize_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(cases: list[EvalCase], client: Any) -> list[EvalResult]:
    """Run every case in ``cases`` against ``client`` and grade the transcripts. Each
    case gets its own freshly seeded store, so cases never interact."""
    return asyncio.run(_run_all(cases, client))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", nargs="*", help="case IDs; defaults to all cases")
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--require-key", action="store_true", help="fail instead of skipping")
    parser.add_argument("--report", type=Path, help="write structured, commit-bound results")
    parser.add_argument("--case-timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.case_timeout_seconds <= 600:
        parser.error("--case-timeout-seconds must be between 1 and 600")
    unknown = set(args.cases) - {case.id for case in CASES}
    if unknown:
        parser.error(f"unknown cases: {', '.join(sorted(unknown))}")

    from host.anthropic_client import build_anthropic_client

    cases = [c for c in CASES if c.id in args.cases] if args.cases else CASES
    root = Path(__file__).resolve().parent.parent

    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=root, text=True, timeout=30).strip()

    async def execute() -> int:
        client = build_anthropic_client()
        if client is None:
            print(
                "No ANTHROPIC_API_KEY set -- skipping the live eval suite; set one to exercise it."
            )
            return 2 if args.require_key or args.report else 0
        report: dict[str, Any] | None = None
        try:
            try:
                report = {
                    "format_version": 1,
                    "commit_sha": git("rev-parse", "HEAD"),
                    "worktree_dirty": bool(git("status", "--porcelain")),
                    "started_at": datetime.now(UTC).isoformat(),
                    "models": {
                        "shopping": shopping_agent_config().model,
                        "merchant": merchant_agent_config().model,
                    },
                    "requested_repetitions": args.repetitions,
                    "case_timeout_seconds": args.case_timeout_seconds,
                    "case_ids": [case.id for case in cases],
                    "runs": [],
                    "passed": False,
                }
                _checkpoint_report(args.report, report)
                for repetition in range(1, args.repetitions + 1):
                    current_run: dict[str, Any] = {"repetition": repetition, "results": []}
                    report["runs"].append(current_run)
                    async for result in _iter_results(
                        cases, client, case_timeout_seconds=args.case_timeout_seconds
                    ):
                        current_run["results"].append(asdict(result))
                        _checkpoint_report(args.report, report)
                        status = "PASS" if result.passed else "FAIL"
                        print(
                            f"[{repetition}/{args.repetitions} {status}] "
                            f"{result.case_id}: {result.reason}",
                            flush=True,
                        )
            finally:
                await client.close()
            # Cleanup must succeed before any checkpoint can be marked passing.
            report["passed"] = all(
                [result["case_id"] for result in run["results"]] == report["case_ids"]
                and all(result["passed"] for result in run["results"])
                for run in report["runs"]
            )
        except BaseException as error:
            if report is not None:
                report["passed"] = False
                # Do not embed provider error messages, headers, or credentials.
                report["failure_type"] = type(error).__name__
            raise
        finally:
            if report is not None:
                report["finished_at"] = datetime.now(UTC).isoformat()
                _checkpoint_report(args.report, report)
        total = sum(len(run["results"]) for run in report["runs"])
        passed = sum(result["passed"] for run in report["runs"] for result in run["results"])
        print(f"\n{passed}/{total} passed")
        return 0 if report["passed"] else 1

    return asyncio.run(execute())


if __name__ == "__main__":
    sys.exit(main())
