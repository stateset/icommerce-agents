"""Fail fast when the environment blocks asyncio's Unix self-pipe transport.

This is a prerequisite check, not a test of engine correctness. It needs no engine,
network listener, credentials, or running event loop.
"""

from __future__ import annotations

import socket
import sys


def check_asyncio_wakeup() -> None:
    """Exercise the nonblocking socket pair used for cross-thread notifications."""
    try:
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.setblocking(False)
            writer.setblocking(False)
            if writer.send(b"\0") != 1 or reader.recv(1) != b"\0":
                raise OSError("socket pair did not deliver the wakeup byte")
    except OSError as exc:
        raise RuntimeError(
            "This environment blocks asyncio's local socket-pair wakeups. "
            "Threaded engine work can finish without waking the event loop, causing "
            "apparent hangs and delayed cancellation. Run in an environment that "
            "permits local socket pairs (request sandbox permission where applicable). "
            f"Underlying error: {exc}"
        ) from exc


def main() -> int:
    try:
        check_asyncio_wakeup()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Asyncio local socket-pair wakeups are permitted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
