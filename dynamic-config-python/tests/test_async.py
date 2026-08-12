"""The asyncio surface: nothing blocking ever runs on the loop."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import DynamicConfig, InvalidError


class Database(BaseModel):
    host: str
    port: int


def write(port: int) -> None:
    Path("config.toml").write_text(f'[db]\nhost = "h"\nport = {port}\n')


async def test_init_load_and_reload_all_have_async_twins(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")

    candidate = await config.load_async()
    assert candidate.port == 1
    assert config.try_current() is None, "load installs nothing, async or not"

    await config.init_async()
    assert config.current().port == 1

    write(2)
    await config.reload_async()
    assert config.current().port == 2


async def test_the_loop_keeps_running_while_a_load_happens(workspace: Path) -> None:
    """The point of the async twins: the loop is never the thing waiting."""
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    ticker = asyncio.create_task(tick())

    for _ in range(20):
        await config.init_async()

    ticker.cancel()

    assert ticks > 0, "the loop was blocked for the whole run"


async def test_changed_async_resolves_on_the_next_install(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    async def reload_soon() -> None:
        await asyncio.sleep(0.15)
        write(2)
        await config.reload_async()

    task = asyncio.create_task(reload_soon())
    model = await config.changed_async(timeout=10)
    await task

    assert model is not None
    assert model.port == 2


async def test_changed_async_times_out_quietly(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    started = time.monotonic()
    result = await config.changed_async(timeout=0.3)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 5, "the timeout has to be the thing that ends the wait"


async def test_changed_async_can_be_cancelled(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    task = asyncio.create_task(config.changed_async())
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    write(2)
    await config.reload_async()
    assert config.current().port == 2, "a cancelled wait leaves the engine alone"


async def test_a_failing_async_load_raises_where_it_was_awaited(
    workspace: Path,
) -> None:
    Path("config.toml").write_text("[db]\nport = 1\n")

    config = DynamicConfig(Database, key="db").file("config.toml")

    with pytest.raises(InvalidError):
        await config.init_async()

    assert config.try_current() is None


async def test_many_configurations_load_concurrently(workspace: Path) -> None:
    for index in range(8):
        Path(f"config-{index}.toml").write_text(f'[db]\nhost = "h"\nport = {index}\n')

    configs = [
        DynamicConfig(Database, key="db").file(f"config-{index}.toml")
        for index in range(8)
    ]

    await asyncio.gather(*(config.init_async() for config in configs))

    assert [config.current().port for config in configs] == list(range(8))


async def test_a_watcher_wakes_an_async_consumer(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    seen: list[int] = []

    async def consume() -> None:
        async for model in config.changes():
            seen.append(model.port)
            if len(seen) == 1:
                return

    consumer = asyncio.create_task(consume())

    with config.watch(debounce=0.05, poll_interval=0.05):
        deadline = asyncio.get_running_loop().time() + 20

        while not seen and asyncio.get_running_loop().time() < deadline:
            write(2)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(consumer, timeout=5)

    assert seen == [2], "the watcher's reload reached the async consumer"


async def test_a_supplied_executor_runs_the_blocking_half(workspace: Path) -> None:
    """The Python-side twin of Rust's `set_blocking_executor`."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    write(1)
    threads: set[str] = set()

    class Named(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            def record():  # type: ignore[no-untyped-def]
                threads.add(threading.current_thread().name)

                return fn(*args, **kwargs)

            return super().submit(record)

    with Named(1, thread_name_prefix="config-pool") as pool:
        config = DynamicConfig(Database, key="db", executor=pool).file("config.toml")
        await config.init_async()

        write(2)
        await config.reload_async()

    assert config.current().port == 2
    assert threads, "the supplied executor was never used"
    assert all(name.startswith("config-pool") for name in threads), threads


async def test_the_process_wide_executor_is_the_fallback(workspace: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import dynamic_config

    write(1)
    seen: set[str] = set()

    class Named(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            def record():  # type: ignore[no-untyped-def]
                seen.add(threading.current_thread().name)

                return fn(*args, **kwargs)

            return super().submit(record)

    with Named(1, thread_name_prefix="global-pool") as pool:
        dynamic_config.set_executor(pool)

        try:
            config = DynamicConfig(Database, key="db").file("config.toml")
            await config.init_async()
        finally:
            dynamic_config.set_executor(None)

    assert config.current().port == 1
    assert all(name.startswith("global-pool") for name in seen), seen


async def test_waiting_does_not_occupy_the_configured_executor(
    workspace: Path,
) -> None:
    """A wait is a parking spot, not work.

    Several `changes()` iterators against a one-worker pool would deadlock
    if the waits went there — so they deliberately do not.
    """
    from concurrent.futures import ThreadPoolExecutor

    write(1)

    with ThreadPoolExecutor(1, thread_name_prefix="tiny") as pool:
        config = DynamicConfig(Database, key="db", executor=pool).file("config.toml")
        await config.init_async()

        # Three waiters and one worker: if the waits used the pool, the
        # reload below could never get a thread to run on.
        waiters = [
            asyncio.create_task(config.changed_async(timeout=10)) for _ in range(3)
        ]
        await asyncio.sleep(0.2)

        write(2)
        await config.reload_async()

        results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=10)

    assert [model.port for model in results if model] == [2, 2, 2]


# ── Starting a watcher, from a loop ────────────────────────────────────
#
# `watch()` is not free: it resolves the directories to observe, registers
# each with the notification backend and spawns the carrier thread. The
# GIL is released for all of it, but a *loop* that makes the call still
# waits for those syscalls, because the loop runs on the calling thread.
# `watch_async` is that call with the wait moved to a worker.


async def test_watch_async_starts_a_watcher_that_reloads(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    watch = await config.watch_async(debounce=0.05, poll_interval=0.05)

    try:
        assert watch.running

        deadline = asyncio.get_running_loop().time() + 20

        while (
            config.current().port == 1 and asyncio.get_running_loop().time() < deadline
        ):
            write(2)
            await asyncio.sleep(0.1)

        assert config.current().port == 2, "the awaited watcher never reloaded"
    finally:
        watch.stop()

    assert not watch.running


async def test_starting_a_watcher_happens_off_the_loop_thread(workspace: Path) -> None:
    """The property the twin exists for, asserted without timing anything."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    write(1)
    started_on: set[str] = set()

    class Named(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            def record():  # type: ignore[no-untyped-def]
                started_on.add(threading.current_thread().name)

                return fn(*args, **kwargs)

            return super().submit(record)

    loop_thread = threading.current_thread().name

    with Named(1, thread_name_prefix="watch-pool") as pool:
        config = DynamicConfig(Database, key="db", executor=pool).file("config.toml")
        await config.init_async()

        started_on.clear()  # init used the pool too; this is about watching
        watch = await config.watch_async(debounce=0.05)
        watch.stop()

    assert started_on, "watch_async never reached the executor"
    assert all(name.startswith("watch-pool") for name in started_on), started_on
    assert loop_thread not in started_on, "the loop thread did the registering"


async def test_starting_a_poll_watcher_leaves_the_loop_working(
    workspace: Path,
) -> None:
    """The expensive case, with the loop still answering afterwards.

    Polling scans everything it watches before it can report a change, so
    over a directory with a few hundred entries the start is milliseconds
    rather than microseconds — the case `watch_async` exists for.

    What this asserts is that the loop still works across the operation
    and that the watcher actually watches. That the registration happens
    somewhere other than the loop thread is asserted exactly, and without
    timing anything, by
    `test_starting_a_watcher_happens_off_the_loop_thread`. Counting how
    often a ticker ran inside a millisecond-scale window would be
    measuring the scheduler, which on a loaded machine says nothing —
    this suite has been bitten by that before.
    """
    write(1)

    for index in range(300):
        Path(f"neighbour-{index}.txt").write_text("x")

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    ticker = asyncio.create_task(tick())
    watch = await config.watch_async(debounce=0.05, poll_interval=0.05)

    try:
        # A window the loop is free in. If starting the watcher had wedged
        # it — a lock held, a thread never handed back — nothing here runs.
        ticks = 0
        await asyncio.sleep(0.05)

        assert ticks > 0, "the loop stopped running once the watcher started"

        deadline = asyncio.get_running_loop().time() + 20

        while (
            config.current().port == 1 and asyncio.get_running_loop().time() < deadline
        ):
            write(2)
            await asyncio.sleep(0.1)

        assert config.current().port == 2, "the polling watcher never reloaded"
    finally:
        ticker.cancel()
        watch.stop()


async def test_stopping_a_watcher_returns_without_waiting_for_anything(
    workspace: Path,
) -> None:
    """`stop()` has no async twin because there is nothing to wait for.

    A debounce window is open here — the file was just written — and a
    `stop()` that joined the watcher thread or drained that window would
    take the debounce with it. It takes a fraction of a millisecond.
    """
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    await config.init_async()

    watch = await config.watch_async(debounce=5.0, poll_interval=0.05)
    write(2)
    await asyncio.sleep(0.2)  # inside the five-second debounce window

    started = time.perf_counter()
    watch.stop()
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"stop() waited {elapsed:.3f}s — it drained the debounce"
    assert not watch.running
