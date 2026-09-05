from pathlib import Path

import pytest

from engine_backend.kernel import KernelClient
from engine_backend.seed import seed_store
from engine_backend.store import EngineStore

CONFIG = Path(__file__).resolve().parent.parent / "config"


def pytest_sessionstart(session):
    from scripts.runtime_check import check_asyncio_wakeup

    try:
        check_asyncio_wakeup()
    except RuntimeError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.fixture
def live_report():
    """Synthetic validation input only; never production evidence."""
    from scripts.live_eval_check import EXPECTED_CASE_IDS

    return {
        "format_version": 1,
        "commit_sha": "a" * 40,
        "worktree_dirty": False,
        "passed": True,
        "models": {"shopping": "test-shopping-model", "merchant": "test-merchant-model"},
        "started_at": "2026-09-04T16:00:00Z",
        "finished_at": "2026-09-04T17:00:00Z",
        "requested_repetitions": 3,
        "case_ids": list(EXPECTED_CASE_IDS),
        "runs": [
            {
                "repetition": index,
                "results": [
                    {"case_id": case_id, "passed": True, "reason": "synthetic passing verdict"}
                    for case_id in EXPECTED_CASE_IDS
                ],
            }
            for index in (1, 2, 3)
        ],
    }


@pytest.fixture
def store(tmp_path):
    """A file-backed seeded store — file-backed so readonly_sql() works."""
    engine_store = EngineStore(str(tmp_path / "store.db"))
    seed_store(engine_store.commerce)
    return engine_store


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")
