"""Create and verify a consistent online backup of an iCommerce SQLite store."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path


def backup_store(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"source database does not exist: {source}")
    if source == destination:
        raise ValueError("backup destination must differ from the source database")
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"backup directory does not exist: {destination.parent}")

    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        with (
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30) as reader,
            sqlite3.connect(temporary, timeout=30) as writer,
        ):
            reader.execute("PRAGMA busy_timeout = 30000")
            reader.backup(writer)
            result = writer.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError(f"backup integrity check failed: {result!r}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="live SQLite store path")
    parser.add_argument("destination", type=Path, help="new backup path (must not exist)")
    arguments = parser.parse_args()
    backup_store(arguments.source, arguments.destination)
    print(f"verified backup written to {arguments.destination}")


if __name__ == "__main__":
    main()
