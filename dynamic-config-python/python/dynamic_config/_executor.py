"""Which thread pool pays for the blocking half of an async call.

One process-wide default, and a per-configuration override. Kept in its
own module so the setting has one home rather than a global that several
modules reach into.
"""

from __future__ import annotations

from concurrent.futures import Executor

_DEFAULT_EXECUTOR: Executor | None = None


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

    Waiting for a reload (``changes()``, ``changed_async``) deliberately
    stays on the loop's default executor whatever this is set to: a wait
    is a parking spot rather than work, and parking several of them in a
    pool sized for work is how that pool starves.
    """
    global _DEFAULT_EXECUTOR

    _DEFAULT_EXECUTOR = executor


def default_executor() -> Executor | None:
    """The current process-wide choice, read at the moment of the call.

    A function rather than the value: importing the global would freeze
    whatever it happened to be at import time, and this is meant to be
    changed at runtime.
    """
    return _DEFAULT_EXECUTOR
