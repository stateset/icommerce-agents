import sqlite3

import pytest

from engine_backend.store import EngineStore
from scripts.backup_store import backup_store


def test_online_backup_contains_durable_control_state(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "backup.db"
    store = EngineStore(str(source))
    store.bind("session-for-backup", "customer-7", "customer")

    backup_store(source, destination)

    with sqlite3.connect(destination) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            backup.execute(
                "SELECT subject_id FROM icommerce_agent_sessions WHERE session_id = ?",
                ("session-for-backup",),
            ).fetchone()[0]
            == "customer-7"
        )


def test_backup_refuses_overwrite_and_source_collision(tmp_path):
    source = tmp_path / "live.db"
    source.touch()
    with pytest.raises(ValueError, match="must differ"):
        backup_store(source, source)
    destination = tmp_path / "existing.db"
    destination.touch()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        backup_store(source, destination)
