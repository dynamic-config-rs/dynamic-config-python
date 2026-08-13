"""Handles that own something, and the sweep that ends them.

A `Watch` owns a running watcher thread; a `HookGuard` owns a
registration. Both are tracked in weak sets so the `atexit` handler can
stop them while the interpreter is still whole — a watcher thread
calling into a torn-down Python is the classic embedding crash, and
Rust's `Drop` deliberately never touches Python.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
import weakref
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable

from . import _core

if TYPE_CHECKING:
    from ._config import DynamicConfig


class Watch:
    """A running watcher. Stopping it — or leaving its ``with`` — ends it."""

    __slots__ = ("__weakref__", "_inner")

    def __init__(self, inner: _core.Watch) -> None:
        """Wraps a running watcher. Obtained from ``config.watch(...)``.

        Registered in a module-level weak set so the ``atexit`` handler
        can stop it before the interpreter finalizes — a watcher thread
        calling into a torn-down Python is the classic embedding crash.
        """
        self._inner = inner
        _register(_LIVE_WATCHES, self)

    @property
    def running(self) -> bool:
        """Whether this watcher is still watching."""
        return bool(self._inner.running)

    def stop(self) -> None:
        """Stops watching. Idempotent.

        Safe to call from a coroutine, and deliberately has no async twin:
        it drops the backend, which closes the channel the watcher thread
        is parked on, and returns without joining. It does not wait out a
        debounce window either — a reload already in flight finishes on
        its own thread. Measured at well under a tenth of a millisecond,
        native and polling alike.
        """
        self._inner.stop()

    def detach(self) -> None:
        """Watches for the rest of the process, whatever happens here.

        Deliberately one-way, and it opts out of one guarantee: the
        `atexit` sweep stops the watchers it can reach, and a detached one
        is no longer reachable — there is no handle left to stop. That is
        the trade the call exists to make, and the engine's watcher is
        built to survive it, but a watcher that must be stopped at exit
        should be held rather than detached.
        """
        self._inner.detach()

    def __enter__(self) -> Watch:
        """Enters a ``with`` block that stops the watcher on the way out."""
        return self

    def __exit__(self, *_exception: object) -> None:
        """Stops the watcher, however the block ended."""
        self.stop()

    def __repr__(self) -> str:
        """Whether it runs — a watcher has no values to leak."""
        return repr(self._inner)


class HookGuard:
    """The registration, and the hook it registered.

    Unregisters when closed, or when its ``with`` ends. It is also
    callable, forwarding to the hook itself — which is what makes the
    decorator form work without taking the function away::

        @config.on_reload
        def resize(old, new):
            pool.resize(new.pool_size)

        resize(None, config.current())   # still the function
        resize.close()                   # and also the registration
    """

    __slots__ = ("_config", "_hook", "_token")

    def __init__(
        self,
        config: _core.Config,
        token: int,
        hook: Callable[..., Any] | None = None,
    ) -> None:
        """Holds the token ``on_reload`` handed back, for unregistering."""
        self._config = config
        self._token = token
        self._hook = hook

    @property
    def hook(self) -> Callable[..., Any] | None:
        """The function this guard registered, if it was given one."""
        return self._hook

    def __call__(self, *arguments: Any, **keywords: Any) -> Any:
        """Calls the registered hook, so a decorated name stays a function."""
        if self._hook is None:
            raise TypeError("this guard was not given a hook to call")

        return self._hook(*arguments, **keywords)

    def close(self) -> None:
        """Unregisters the hook. Idempotent."""
        self._config.remove_hook(self._token)

    def __enter__(self) -> HookGuard:
        """Enters a ``with`` block that unregisters on the way out."""
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Unregisters the hook, however the block ended."""
        self.close()

    def __repr__(self) -> str:
        """The token, which is all a guard is."""
        return f"<HookGuard token={self._token}>"


_LIVE_CONFIGS: weakref.WeakSet[DynamicConfig[Any]] = weakref.WeakSet()
_LIVE_WATCHES: weakref.WeakSet[Watch] = weakref.WeakSet()

#: Guards both sets above.
#:
#: `WeakSet` is only as thread-safe as the GIL makes it: its `add` is
#: several bytecodes over an internal `set` plus a pending-removals list,
#: and on a free-threaded interpreter two threads registering
#: configurations at the same time have nothing serialising them. Under
#: the GIL this lock costs a few hundred nanoseconds once per object;
#: without one, a `WeakSet` mutated while the `atexit` sweep iterates it
#: is exactly the crash this whole subsystem exists to prevent.
#:
#: Held only around the mutation and around the *snapshot* the sweep
#: takes — never while a watcher is stopped, which would put a lock on
#: the path a weakref callback can run on.
_REGISTRY = threading.Lock()


def _register(registry: weakref.WeakSet[Any], live: Any) -> None:
    """Adds ``live`` to ``registry`` under the lock."""
    with _REGISTRY:
        registry.add(live)


def _snapshot(registry: weakref.WeakSet[Any]) -> list[Any]:
    """Everything currently in ``registry``, as a plain list."""
    with _REGISTRY:
        return list(registry)


# ── Interpreter shutdown ───────────────────────────────────────────────


@atexit.register
def _release_everything() -> None:
    """Stops every watcher and drops every cached model, before teardown.

    A watcher thread that outlives finalization would call into a Python
    that is no longer there — the classic embedding crash. Stopping the
    threads here, while the interpreter is still whole, is what makes that
    impossible rather than unlikely.
    """
    for watch in _snapshot(_LIVE_WATCHES):
        # Teardown is best effort: an object already torn down by an
        # earlier handler must not stop the ones after it.
        with contextlib.suppress(Exception):
            watch.stop()

    for config in _snapshot(_LIVE_CONFIGS):
        with contextlib.suppress(Exception):
            config._core.release()
