"""Where a callback runs, and what happens when they pile up.

Two enumerations rather than two bare strings. They subclass `str`, so
``dispatch="executor"`` keeps working and a value read out of a
configuration file is accepted as it is; they are `Enum`s, so a typo is a
`ValueError` at registration rather than a callback that silently never
runs.

`StrEnum` would say this in one word, and it arrived in 3.11. The floor
here is 3.9.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["Backpressure", "Dispatch"]


class Dispatch(str, Enum):
    """Which thread — or loop — a reload hook runs on."""

    INLINE = "inline"
    """On the thread that installed, before the reload returns.

    The default, and the only one that existed before 0.2: a hook is part
    of the reload. Cheap work belongs here; anything slow delays the next
    install, and for a watcher that means delaying the watcher.
    """

    EXECUTOR = "executor"
    """On the configuration executor, off the installing thread.

    For work that is slow but synchronous — rebuilding a pool, rewriting a
    template. The reload returns without waiting for it.
    """

    ASYNCIO = "asyncio"
    """As a task on the event loop that registered the hook.

    For a coroutine function, and the only value that accepts one. The
    watcher schedules it with `call_soon_threadsafe` and moves on: reload
    latency and callback latency are separate numbers.
    """

    @classmethod
    def _missing_(cls, value: object) -> Dispatch:
        raise ValueError(
            f"{value!r} is not a dispatch: use one of "
            f"{', '.join(repr(member.value) for member in cls)}"
        )


class Backpressure(str, Enum):
    """What a hook does when installs arrive faster than it finishes."""

    EVERY = "every"
    """One call per install, in order. The default for `Dispatch.INLINE`,
    where it is the only thing that can happen anyway."""

    LATEST = "latest"
    """Coalesce: while a call is running, keep only the newest install and
    run it next. The default for `EXECUTOR` and `ASYNCIO`.

    The right shape for configuration — resizing a pool to a size nobody
    is asking for any more is work done for nothing — and the same rule
    `changes()` already follows.
    """

    SERIAL = "serial"
    """Queue every install and run them one at a time, dropping nothing.

    For a hook that is a log rather than a reconciliation: an audit trail
    with a gap in it is a broken audit trail.
    """

    CANCEL_PREVIOUS = "cancel_previous"
    """A new install cancels the call still running, then starts its own.

    `Dispatch.ASYNCIO` only — a thread cannot be cancelled. For a hook
    whose work is worthless the moment it is superseded, such as opening a
    connection to an address that has just changed again.
    """

    @classmethod
    def _missing_(cls, value: object) -> Backpressure:
        raise ValueError(
            f"{value!r} is not a backpressure policy: use one of "
            f"{', '.join(repr(member.value) for member in cls)}"
        )
