import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine_backend.kernel import KernelClient
from engine_backend.store import EngineStore

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


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


@pytest.fixture(scope="session")
def engine_template(tmp_path_factory) -> Path:
    """One seeded engine file per test session.

    Opening ``Commerce`` on a *new* file runs the engine's own migrations and costs
    about 2.5 seconds; reopening an existing file costs a quarter of that. So the
    template is built once, in a subprocess so every handle (and the WAL) is closed
    on exit, and each test copies the file instead of paying the first-open cost.
    """
    path = tmp_path_factory.mktemp("engine-template") / "template.db"
    script = (
        "import sys; from engine_backend.store import EngineStore; "
        "from engine_backend.seed import seed_store; "
        "seed_store(EngineStore(sys.argv[1]).commerce)"
    )
    subprocess.run([sys.executable, "-c", script, str(path)], check=True, cwd=ROOT)
    assert path.exists() and not path.with_name("template.db-wal").exists()
    return path


@pytest.fixture
def engine_db(tmp_path, engine_template):
    """``engine_db("name.db")`` -> path to a fresh, seeded copy of the template."""

    def make(name: str = "store.db") -> str:
        target = tmp_path / name
        shutil.copyfile(engine_template, target)
        return str(target)

    return make


@pytest.fixture
def db_path(engine_db) -> str:
    return engine_db()


@pytest.fixture
def store(db_path):
    """A file-backed seeded store — file-backed so readonly_sql() works."""
    return EngineStore(db_path)


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")
