from pathlib import Path

import pytest

from engine_backend.kernel import KernelClient
from engine_backend.seed import seed_store
from engine_backend.store import EngineStore

CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def store(tmp_path):
    """A file-backed seeded store — file-backed so readonly_sql() works."""
    engine_store = EngineStore(str(tmp_path / "store.db"))
    seed_store(engine_store.commerce)
    return engine_store


@pytest.fixture
def kernel(store):
    return KernelClient(store, CONFIG / "kernel-policy.json", CONFIG / "kernel-principal.json")
