"""Shared asyncio helpers — bounded waits and task-result consumption.

These encode idioms that were previously hand-rolled in many modules with
subtly different timeout/cancellation semantics:
- ``consume_task_result`` drains a finished task's exception so the event
  loop never reports it as an unhandled error.
- ``wait_with_timeout`` awaits an operation up to a deadline, cancelling
  and consuming it on timeout or on caller cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


def consume_task_result(task: asyncio.Future[Any]) -> None:
    """Read a task's exception so it is never reported as unhandled.

    Safe to call on pending, done, and cancelled tasks.
    """
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


async def wait_with_timeout(
    operation: Awaitable[T] | asyncio.Future[T], timeout_seconds: float
) -> bool:
    """Await *operation* up to *timeout_seconds*.

    Returns True if it completed within the deadline (callers then await
    the operation to obtain its result or exception), False on timeout.
    On timeout — or when the caller is cancelled — the operation is
    cancelled and its result consumed, so nothing keeps running or is left
    as an unhandled exception.
    """
    future: asyncio.Future[T] = (
        operation
        if isinstance(operation, asyncio.Future)
        else asyncio.ensure_future(operation)
    )
    try:
        done, _ = await asyncio.wait([future], timeout=timeout_seconds)
    except asyncio.CancelledError:
        future.cancel()
        future.add_done_callback(consume_task_result)
        raise
    if not done:
        future.cancel()
        future.add_done_callback(consume_task_result)
        return False
    return True
