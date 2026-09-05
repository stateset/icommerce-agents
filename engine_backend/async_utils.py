"""Keep synchronous engine work inside its async ownership boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import anyio


async def complete_before_cancelling[T](work: Awaitable[T]) -> T:
    """Defer caller cancellation until work finishes, then propagate cancellation.

    Cancelling ``to_thread`` cannot stop its thread. Releasing a lock or lease
    before that thread exits would let another caller race an unfinished write.
    Shield every wait, including repeated cancellation during disconnect cleanup.
    """
    task = asyncio.ensure_future(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Starlette uses AnyIO cancellation scopes, which cancel every checkpoint.
        # Shield that scope too, so disconnect cleanup does not spin indefinitely.
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
        if not task.cancelled():
            task.exception()
        raise
