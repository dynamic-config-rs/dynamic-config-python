"""Which thread pool pays for the blocking half of an async call.

One process-wide default, and a per-configuration override. Kept in its
own module so the setting has one home rather than a global that several
modules reach into.

Nothing here pays for *waiting*. Since 0.2 a reload is awaited on a
notifier thread owned by the configuration (`_notify`), not by parking an
executor slot — which is why a pool of two is a sensible size even for a
service with fifty awaiting tasks.
"""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager

__all__ = ["configure_executor", "default_executor", "executor", "set_executor"]

_DEFAULT_EXECUTOR: Executor | None = None

#: Whether the pool above is one this module built. A pool the caller
#: handed over is theirs to shut down — replacing it must not close
#: something the rest of their program is still using.
_OWNED: Executor | None = None

_LOCK = threading.Lock()


def set_executor(executor: Executor | None) -> None:
    """Where the blocking half of an async call runs, process-wide.

    Reading and parsing files is blocking work; the ``_async`` methods
    hand it to an executor so the event loop is never the thing waiting.
    By default that is the loop's own — which is shared with every other
    ``run_in_executor`` call in the process, and can therefore be busy.
    A service that would rather not queue behind an unrelated batch job
    gives configuration its own:

        from concurrent.futures import ThreadPoolExecutor
        import dynamic_config

        dynamic_config.set_executor(ThreadPoolExecutor(2, thread_name_prefix="config"))

    This is the Python-side twin of the Rust crate's
    ``set_blocking_executor``, and it answers the same question: *which
    pool pays for the blocking part*. Pass ``None`` to go back to the
    loop's default. A single configuration can override it with the
    ``executor`` argument to :class:`DynamicConfig`.

    The pool passed here belongs to the caller: it is never shut down by
    this library, including at exit. :func:`configure_executor` is the
    version that builds one and owns it.

    Waiting for a reload — ``changes()``, ``changed_async()``,
    ``events()`` — uses no executor at all, so sizing this pool is about
    loads and refreshes only.
    """
    global _DEFAULT_EXECUTOR, _OWNED

    with _LOCK:
        previous, _DEFAULT_EXECUTOR, _OWNED = _OWNED, executor, None

    if previous is not None and previous is not executor:
        previous.shutdown(wait=False)


def configure_executor(
    workers: int = 2,
    *,
    thread_name_prefix: str = "dynamic-config",
) -> Executor:
    """Builds the configuration pool, and owns it.

        dynamic_config.configure_executor(4)

    The two-line version of the ``ThreadPoolExecutor`` in
    :func:`set_executor`, with the two details that are easy to leave
    out: the threads are named — a dump that says
    ``dynamic-config-blocking-0`` answers a question ``ThreadPoolExecutor-3_0``
    does not — and the pool is shut down at interpreter exit, so it never
    outlives the program that built it.

    Calling it twice replaces the pool and shuts the previous one down.
    Returns the pool, for a caller who wants to submit to it too.
    """
    if workers < 1:
        raise ValueError("a pool of no threads cannot run anything")

    pool = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"{thread_name_prefix}-blocking",
    )

    global _DEFAULT_EXECUTOR, _OWNED

    with _LOCK:
        previous, _DEFAULT_EXECUTOR, _OWNED = _OWNED, pool, pool

    if previous is not None:
        previous.shutdown(wait=False)

    return pool


@contextmanager
def executor(
    pool: Executor | None = None,
    *,
    workers: int = 2,
) -> Iterator[Executor | None]:
    """The pool as a block, restored on the way out.

        with dynamic_config.executor(workers=4):
            await config.init_async()

    For a test that wants its own pool, and for a script whose async
    section is one phase of a longer program. A pool this builds is shut
    down at the end of the block; a pool passed in is left alone, because
    it is the caller's.
    """
    global _DEFAULT_EXECUTOR, _OWNED

    with _LOCK:
        saved, saved_owned = _DEFAULT_EXECUTOR, _OWNED

    built = pool is None
    chosen = (
        ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="dynamic-config-blocking"
        )
        if built
        else pool
    )

    with _LOCK:
        _DEFAULT_EXECUTOR, _OWNED = chosen, chosen if built else None

    try:
        yield chosen
    finally:
        with _LOCK:
            _DEFAULT_EXECUTOR, _OWNED = saved, saved_owned

        if built and chosen is not None:
            chosen.shutdown(wait=False)


def default_executor() -> Executor | None:
    """The current process-wide choice, read at the moment of the call.

    A function rather than the value: importing the global would freeze
    whatever it happened to be at import time, and this is meant to be
    changed at runtime.
    """
    return _DEFAULT_EXECUTOR


@atexit.register
def _shutdown_owned() -> None:
    """Shuts down a pool this module built, and only such a pool."""
    global _OWNED

    with _LOCK:
        pool, _OWNED = _OWNED, None

    if pool is not None:
        pool.shutdown(wait=False)
