"""The bridge between a reload and an event loop.

Before 0.2 an awaiting task polled: `run_in_executor` a quarter-second
wait, come back, submit again. It worked, and it cost one repeating
executor submission *per waiter* — a hundred of them for fifty
configurations with two consumers each — for the sole purpose of noticing
cancellation within 250 ms.

This is what replaced it:

    Rust watcher → install → generation++ → notify
                                              │
                     one notifier thread per configuration, shared
                                              │
                            loop.call_soon_threadsafe(future.set_result)
                                              │
                                        awaiting tasks

One parked thread per *configuration that has async consumers*, not per
consumer, and no polling at all. Cancellation is immediate: a cancelled
task drops its future and stops awaiting on the spot.

The thread ends at the first install that finds nobody waiting. Until
that install, a configuration whose consumers have all gone keeps one
thread parked on a condition variable — no timer, no wake-ups, no CPU,
and no way to reclaim it earlier without introducing exactly the polling
this replaced.

The thread parks in `wait_for_change` with the GIL released. It can be
woken by two things and only two: an install, or `release()` — which is
why `Wake` carries a `closed` flag on the Rust side.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import _core

__all__ = ["EventNotifier", "Notifier"]


class Notifier:
    """One per configuration, started on the first await and ended by the last."""

    def __init__(self, core: _core.Config, key: str) -> None:
        self._core = core
        self._key = key
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = []
        self._thread: threading.Thread | None = None
        self._closed = False

    def wait(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[Any]:
        """A future resolved with ``(generation, model)`` on the next install.

        The generation rides along because a waiter cannot otherwise tell
        *which* install woke it. A consumer that noticed an install by
        reading the generation itself, and then registers here, is still
        holding a registration the notifier is about to resolve for that
        same install — and would report it twice. The number is what
        makes that a comparison rather than a race.

        ``None`` instead of a pair means the configuration was released.
        """
        future: asyncio.Future[Any] = loop.create_future()

        with self._lock:
            if self._closed:
                future.set_result(None)
                return future

            self._waiters.append((loop, future))
            self._start_locked()

        return future

    def _start_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        # The baseline is read HERE — on the caller's thread, under the
        # registration lock, before `wait` returns — never on the new
        # thread. Read there, an install landing between the caller's
        # own generation check and the thread's first instruction would
        # be folded into the baseline and never reported: the lost
        # wake-up this module exists to prevent.
        baseline = int(self._core.generation)

        # Named after the configuration: a thread dump that says
        # `dynamic-config-notify-db` answers a question `Thread-17` does not.
        self._thread = threading.Thread(
            target=self._run,
            args=(baseline,),
            name=f"dynamic-config-notify-{self._key}",
            daemon=True,
        )
        self._thread.start()

    def _run(self, seen: int) -> None:
        while True:
            # No timeout: the wait ends on an install or on `release`, and
            # nothing else. This is the whole difference from polling.
            result = self._core.wait_for_change(seen, None)

            if result is None:
                # `release()` — the configuration is going away.
                self._resolve_all(None)
                return

            seen = int(result[0])
            self._resolve_all((seen, result[1]))

            with self._lock:
                if not self._waiters:
                    # Nobody is listening any more; the next `wait()` starts
                    # a fresh thread. A parked thread per idle configuration
                    # is exactly what this design is avoiding.
                    self._thread = None
                    return

    def _resolve_all(self, wake: Any) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []

        for loop, future in waiters:
            # `call_soon_threadsafe` is the only safe way to touch a future
            # from another thread — and it raises if the loop has closed,
            # which for a task nobody is awaiting any more is nothing to
            # report.
            with contextlib.suppress(RuntimeError):  # pragma: no cover
                loop.call_soon_threadsafe(_settle, future, wake)

    def close(self) -> None:
        """Ends every waiter. `release()` on the Rust side does the rest."""
        with self._lock:
            self._closed = True

        self._resolve_all(None)


def _settle(future: asyncio.Future[Any], wake: Any) -> None:
    if not future.done():
        future.set_result(wake)


class EventNotifier:
    """The :class:`Notifier`, widened to refusals — for ``events()``.

    Same shape, one difference: the thread parks in ``wait_for_event``,
    which the Rust side wakes for an install *or* a refused reload. A
    waiter's future resolves with ``(generation, refusals, model)`` —
    ``model`` is ``None`` when only the refusal counter moved — or with
    ``None`` alone when the configuration was released.

    A separate class rather than a mode on :class:`Notifier`, so a
    configuration with only ``changes()`` consumers keeps a thread that
    refusals never wake.
    """

    def __init__(self, core: _core.Config, key: str) -> None:
        self._core = core
        self._key = key
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[Any]]] = []
        self._thread: threading.Thread | None = None
        self._closed = False

    def wait(self, loop: asyncio.AbstractEventLoop) -> asyncio.Future[Any]:
        """A future resolved on the next install or refusal."""
        future: asyncio.Future[Any] = loop.create_future()

        with self._lock:
            if self._closed:
                future.set_result(None)
                return future

            self._waiters.append((loop, future))
            self._start_locked()

        return future

    def _start_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        # The same baseline discipline as :class:`Notifier`: read on the
        # caller's thread under the registration lock, so nothing that
        # happens after a waiter's own check can fold into the baseline.
        baseline = int(self._core.generation)
        baseline_refusals = int(self._core.refusals)

        self._thread = threading.Thread(
            target=self._run,
            args=(baseline, baseline_refusals),
            name=f"dynamic-config-events-{self._key}",
            daemon=True,
        )
        self._thread.start()

    def _run(self, seen: int, seen_refusals: int) -> None:
        while True:
            result = self._core.wait_for_event(seen, seen_refusals, None)

            if result is None:
                self._resolve_all(None)
                return

            generation, refusals, model = result
            seen = int(generation)
            seen_refusals = int(refusals)
            self._resolve_all((seen, seen_refusals, model))

            with self._lock:
                if not self._waiters:
                    self._thread = None
                    return

    def _resolve_all(self, wake: Any) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []

        for loop, future in waiters:
            with contextlib.suppress(RuntimeError):  # pragma: no cover
                loop.call_soon_threadsafe(_settle, future, wake)

    def close(self) -> None:
        """Ends every waiter. ``release()`` on the Rust side does the rest."""
        with self._lock:
            self._closed = True

        self._resolve_all(None)
