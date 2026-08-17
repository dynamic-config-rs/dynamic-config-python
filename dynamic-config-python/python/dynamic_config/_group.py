"""Several configurations, one lifecycle.

A service with five configurations currently writes the orchestration
itself: init each, watch each, remember every handle, stop them in the
right order on the way out. None of that is application logic, and all of
it is the same in every application. This is that loop, written once.

The group owns *lifecycle*, not storage. `db.current()` is still the read
path — the group never sits between a program and its values, because the
read path is the one thing in this library that has to stay free.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Iterator, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._config import DynamicConfig
    from ._lifetime import Watch
    from ._telemetry import ConfigStatus

__all__ = ["ConfigGroup"]


class ConfigGroup:
    """A lifecycle for several configurations at once.

    Its members are held as ``DynamicConfig[Any]`` and there is no way for
    them not to be: a group is heterogeneous by definition, and Python
    has no way to write "a tuple of configurations, each of a different
    model". That costs nothing, because the group is not the read path —
    ``database.current()`` is, on the object you already have, and that
    one is still `DynamicConfig[Database]`.

        group = ConfigGroup(db, redis, kafka)

        group.init()

        with group.watch():
            serve()

    Async twins for every method, and an async context manager for the
    watch::

        await group.init_async()

        async with group.watching_async():
            await serve()
    """

    def __init__(
        self,
        *configs: DynamicConfig[Any],
        concurrency: int | None = None,
    ) -> None:
        """Builds the group; nothing is loaded here.

        Parameters:
            configs: the configurations, in the order they should start.
            concurrency: how many may load at once. `None` — the default —
                loads them one at a time, which is what a handful of small
                files wants. A number bounds the parallelism for a group
                whose members read from slow places; it never becomes one
                thread per configuration.
        """
        if not configs:
            raise ValueError("a group of no configurations has nothing to do")

        keys = [config.key for config in configs]
        duplicates = {key for key in keys if keys.count(key) > 1}

        if duplicates:
            raise ValueError(
                "two configurations in one group share a key, and a group "
                f"reports per key: {', '.join(sorted(duplicates))}"
            )

        if concurrency is not None and concurrency < 1:
            raise ValueError("concurrency is how many may load at once, so at least 1")

        self._configs = tuple(configs)
        self._concurrency = concurrency
        self._watches: list[Watch] = []

    @property
    def configs(self) -> tuple[DynamicConfig[Any], ...]:
        """The members, in the order they were given."""
        return self._configs

    # ── lifecycle ─────────────────────────────────────────────────────

    def init(self) -> None:
        """Loads every member. The first failure stops the group.

        Members that had already loaded when a later one refused keep
        what they loaded — this is not :meth:`reload_atomic`, and does not
        pretend to be. Calling it again after fixing the offending source
        loads every member once more, exactly as
        :meth:`DynamicConfig.init` does on its own.
        """
        self._each(lambda config: config.init(), stop_on_failure=True)

    async def init_async(self) -> None:
        """`init`, off the event loop, bounded by `concurrency`."""
        await self._each_async(lambda config: config.init_async(), stop_on_failure=True)

    def reload(self) -> None:
        """Reloads every member, independently.

        A member that refuses keeps its previous document — the engine's
        rule, unchanged — and the others still reload, so every member has
        its turn and the first failure is raised at the end rather than in
        the middle. For all-or-nothing, use :meth:`reload_atomic`.
        """
        self._each(lambda config: config.reload(), stop_on_failure=False)

    async def reload_async(self) -> None:
        """`reload`, off the event loop, bounded by `concurrency`."""
        await self._each_async(
            lambda config: config.reload_async(), stop_on_failure=False
        )

    def reload_atomic(self) -> None:
        """Every member validates, or no member installs.

        The mixed state this exists to prevent: a deployment moves three
        files, two parse and one does not, and the process runs on two new
        documents and one old one — with nothing in any of them saying so.

            group.reload_atomic()

        Every member loads and validates first; only when all of them have
        does any of them install. A refusal names the member it came from
        and leaves every snapshot exactly as it was, generation included.

        The engine has done this for Rust callers since 0.4
        (`ReloadGroup`); this is the same prepare-then-commit, driven from
        Python.
        """
        prepared: list[tuple[DynamicConfig[Any], int]] = []

        try:
            for config in self._configs:
                prepared.append((config, config._core.prepare()))
        except Exception:
            # Dropping a prepared commit is exactly what *not* applying it
            # means — the same thing the Rust group does by letting the
            # closures fall out of scope.
            self._discard(prepared)

            raise

        while prepared:
            config, token = prepared.pop(0)

            try:
                config._core.commit(token)
            except Exception:
                # Only a bug reaches here — a commit is an install of
                # something already validated. What must not also happen is
                # the rest of the group being left prepared for ever.
                self._discard(prepared)

                raise

    @staticmethod
    def _discard(prepared: Sequence[tuple[DynamicConfig[Any], int]]) -> None:
        """Drops every prepared commit in ``prepared``, installing none."""
        for config, token in prepared:
            config._core.discard(token)

    async def reload_atomic_async(self) -> None:
        """`reload_atomic`, with the loading half off the event loop."""
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(None, self.reload_atomic)

    # ── watching ──────────────────────────────────────────────────────

    def watch(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> ConfigGroup:
        """Starts a watcher for every member.

        Usable as a context manager, which is the shape that cannot leak::

            with group.watch():
                serve()
        """
        for config in self._configs:
            self._watches.append(config.watch(debounce, poll_interval))

        return self

    async def watch_async(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> ConfigGroup:
        """`watch`, off the event loop."""
        for config in self._configs:
            self._watches.append(await config.watch_async(debounce, poll_interval))

        return self

    def stop(self) -> None:
        """Stops every watcher this group started. Idempotent."""
        while self._watches:
            self._watches.pop().stop()

    def __enter__(self) -> ConfigGroup:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.stop()

    @contextmanager
    def watching(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> Iterator[ConfigGroup]:
        """`watch` as a block, stopped on the way out even on an exception."""
        self.watch(debounce, poll_interval)

        try:
            yield self
        finally:
            self.stop()

    @asynccontextmanager
    async def watching_async(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> AsyncIterator[ConfigGroup]:
        """The same, for a service that starts its watchers on the loop.

        ::

            async with group.watching_async():
                await serve()
        """
        await self.watch_async(debounce, poll_interval)

        try:
            yield self
        finally:
            self.stop()

    @contextmanager
    def running(
        self,
        watch: bool = True,
        debounce: float = 0.25,
        poll_interval: float | None = None,
    ) -> Iterator[ConfigGroup]:
        """init, then watch, then stop — the whole lifetime as one block."""
        self.init()

        with ExitStack() as stack:
            if watch:
                stack.enter_context(self.watching(debounce, poll_interval))

            yield self

    @asynccontextmanager
    async def running_async(
        self,
        watch: bool = True,
        debounce: float = 0.25,
        poll_interval: float | None = None,
    ) -> AsyncIterator[ConfigGroup]:
        """The shape a FastAPI ``lifespan`` wants.

        ::

            @asynccontextmanager
            async def lifespan(app):
                async with group.running_async():
                    yield
        """
        await self.init_async()

        async with AsyncExitStack() as stack:
            if watch:
                await stack.enter_async_context(
                    self.watching_async(debounce, poll_interval)
                )

            yield self

    # ── diagnostics ───────────────────────────────────────────────────

    def status(self) -> dict[str, ConfigStatus]:
        """Every member's status, by key — one call for a health endpoint."""
        return {config.key: config.status() for config in self._configs}

    def generations(self) -> dict[str, int]:
        """Every member's generation, by key.

        Equal numbers are not a promise of anything: members reload
        independently unless :meth:`reload_atomic` installed them.
        """
        return {config.key: config.generation for config in self._configs}

    def __len__(self) -> int:
        return len(self._configs)

    def __iter__(self) -> Iterator[DynamicConfig[Any]]:
        return iter(self._configs)

    def __repr__(self) -> str:
        keys = ", ".join(config.key for config in self._configs)

        return f"ConfigGroup({keys})"

    # ── internals ─────────────────────────────────────────────────────

    def _each(
        self,
        work: Callable[[DynamicConfig[Any]], None],
        *,
        stop_on_failure: bool,
    ) -> None:
        """Runs ``work`` over every member, and raises the first failure.

        ``stop_on_failure`` is the difference between *loading a group* and
        *reloading one*: a member that cannot load leaves the group without
        that configuration and there is no point starting the rest, while a
        member that cannot reload is still serving what it had and its
        neighbours' reloads are none of its business.
        """
        if self._concurrency is None:
            failures: list[BaseException] = []

            for config in self._configs:
                try:
                    work(config)
                except Exception as error:
                    if stop_on_failure:
                        raise

                    failures.append(error)

            if failures:
                raise failures[0]

            return

        self._in_parallel(
            [_bind(work, config) for config in self._configs],
            stop_on_failure=stop_on_failure,
        )

    async def _each_async(
        self,
        work: Callable[[DynamicConfig[Any]], Awaitable[None]],
        *,
        stop_on_failure: bool,
    ) -> None:
        """:meth:`_each`, awaited, bounded by ``concurrency``."""
        if self._concurrency is None:
            failures: list[BaseException] = []

            for config in self._configs:
                try:
                    await work(config)
                except Exception as error:
                    if stop_on_failure:
                        raise

                    failures.append(error)

            if failures:
                raise failures[0]

            return

        semaphore = asyncio.Semaphore(self._concurrency)
        stopping = asyncio.Event()

        async def one(config: DynamicConfig[Any]) -> None:
            if stop_on_failure and stopping.is_set():
                return

            async with semaphore:
                try:
                    await work(config)
                except BaseException:
                    if stop_on_failure:
                        stopping.set()

                    raise

        # Gathered rather than raised through: a task left running after
        # its sibling failed would install into a group nobody is waiting
        # on any more, which is the mixed state the atomic reload exists
        # to avoid producing by accident.
        outcomes = await asyncio.gather(
            *(one(config) for config in self._configs), return_exceptions=True
        )

        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    def _in_parallel(
        self, work: Sequence[Callable[[], None]], *, stop_on_failure: bool
    ) -> None:
        """Runs ``work`` on at most ``concurrency`` threads; first error wins."""
        errors: list[BaseException] = []
        lock = threading.Lock()
        pending = list(work)

        def worker() -> None:
            while True:
                with lock:
                    if not pending or (errors and stop_on_failure):
                        return

                    job = pending.pop(0)

                try:
                    job()
                except BaseException as error:
                    with lock:
                        errors.append(error)

                    if stop_on_failure:
                        return

        threads = [
            threading.Thread(target=worker, name="dynamic-config-load", daemon=True)
            for _ in range(min(self._concurrency or 1, len(pending)))
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        if errors:
            raise errors[0]


def _bind(
    work: Callable[[DynamicConfig[Any]], None], config: DynamicConfig[Any]
) -> Callable[[], None]:
    """``work`` with its configuration already chosen."""
    return lambda: work(config)
