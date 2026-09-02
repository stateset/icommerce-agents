import pytest

from engine_backend.seed import seed_store
from engine_backend.store import EngineStore


@pytest.fixture
def store(tmp_path):
    """A file-backed seeded store — file-backed so readonly_sql() works."""
    engine_store = EngineStore(str(tmp_path / "store.db"))
    seed_store(engine_store.commerce)
    return engine_store
