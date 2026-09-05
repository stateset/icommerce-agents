"""Process-lifetime exclusion for agent and merchant work on the local filesystem."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
from pathlib import Path


class TurnLocks:
    """Retain flock descriptors until the owning operation has fully drained.

    Lease expiry alone cannot distinguish a crashed worker from a paused worker.
    flock survives pauses and is released by the OS on process exit. Lock files
    must never be unlinked while workers run: replacing an inode splits ownership.
    Names contain only hashes, never session identifiers.
    """

    def __init__(self, db_path: str) -> None:
        self.directory = Path(str(Path(db_path).resolve()) + ".turn-locks")
        self._owners: dict[str, int] = {}
        self._mutex = threading.Lock()

    def acquire(self, session_id: str, role: str, owner: str) -> bool:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = hashlib.sha256(f"{role}\0{session_id}".encode()).hexdigest()
        fd = os.open(
            self.directory / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        with self._mutex:
            self._owners[owner] = fd
        return True

    def release(self, owner: str) -> None:
        with self._mutex:
            fd = self._owners.pop(owner, None)
        if fd is not None:
            os.close(fd)
