"""Internal polling helper backing the ``wait`` flags on long-running operations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .errors import WaitTimeoutError

logger = logging.getLogger("infersoft")

#: Deadline clock, aliased so tests can fake wait deadlines without patching
#: the global time module (which the auth token cache and event loop also use).
_monotonic = time.monotonic

#: Default seconds between polls for the ``wait`` helpers.
DEFAULT_POLL_INTERVAL = 2.0

#: Default wait budget for job waits (``jobs.wait``, ``jobs.run(wait=True)``,
#: ``client.extract``). Finite on purpose: a stuck job raises
#: ``WaitTimeoutError`` instead of hanging forever (a future unknown terminal
#: status would otherwise poll silently for good). Pass
#: ``max_wait_seconds=None`` to wait without a deadline.
DEFAULT_JOB_WAIT_SECONDS = 1800.0

#: Default wait budget for document-readiness waits (``wait_until_ready``,
#: ``upload(wait=True)``, ``upload_many(wait=True)``). Same rationale and
#: ``None`` escape hatch as ``DEFAULT_JOB_WAIT_SECONDS``.
DEFAULT_DOCUMENT_WAIT_SECONDS = 600.0

T = TypeVar("T")


def warn_once_unknown_status(seen: set[str], value: str, known: frozenset[str], what: str) -> None:
    """Log one warning per distinct unknown status value seen by a wait loop.

    Extensible enums accept statuses this SDK build does not know about; a wait
    loop cannot tell a new non-terminal status (keep polling) from a new
    terminal one (would poll until timeout). This makes the latter diagnosable
    from logs without spamming once per poll. ``seen`` is the caller's
    per-wait-call dedupe set; ``known`` is the enum's known values.
    """
    if value in known or value in seen:
        return
    seen.add(value)
    logger.warning(
        "unknown %s status %r observed while waiting; treating it as non-terminal "
        "(this SDK build may predate it) — polling continues until done or timeout",
        what,
        value,
    )


def poll_until(
    fetch: Callable[[], T],
    done: Callable[[T], bool],
    *,
    max_wait_seconds: float | None,
    poll_interval: float,
    timeout_message: str,
) -> T:
    """Poll ``fetch`` until ``done`` returns true, then return its last value.

    ``max_wait_seconds`` of ``None`` or ``0`` (or negative) waits indefinitely;
    otherwise a :class:`WaitTimeoutError` is raised once the budget is exceeded.
    The condition is always re-checked before raising, so a result that arrives
    right at the deadline still wins.
    """
    deadline: float | None = None
    if max_wait_seconds and max_wait_seconds > 0:
        deadline = _monotonic() + max_wait_seconds
    while True:
        value = fetch()
        if done(value):
            return value
        if deadline is not None and _monotonic() >= deadline:
            raise WaitTimeoutError(timeout_message)
        time.sleep(poll_interval)


async def apoll_until(
    fetch: Callable[[], Awaitable[T]],
    done: Callable[[T], bool],
    *,
    max_wait_seconds: float | None,
    poll_interval: float,
    timeout_message: str,
) -> T:
    """Async twin of :func:`poll_until`; awaits ``fetch`` and sleeps with asyncio."""
    deadline: float | None = None
    if max_wait_seconds and max_wait_seconds > 0:
        deadline = _monotonic() + max_wait_seconds
    while True:
        value = await fetch()
        if done(value):
            return value
        if deadline is not None and _monotonic() >= deadline:
            raise WaitTimeoutError(timeout_message)
        await asyncio.sleep(poll_interval)
