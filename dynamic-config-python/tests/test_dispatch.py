"""Where a hook runs, and what happens when installs outrun it."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import Backpressure, Dispatch, DynamicConfig


class Database(BaseModel):
    host: str
    port: int


def write(port: int) -> None:
    Path("config.toml").write_text(f'[db]\nhost = "h"\nport = {port}\n')


def configured() -> DynamicConfig[Database]:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    return config


def test_a_typo_in_a_dispatch_is_refused_at_registration() -> None:
    with pytest.raises(ValueError, match="is not a dispatch"):
        Dispatch("excutor")

    with pytest.raises(ValueError, match="is not a backpressure policy"):
        Backpressure("newest")


def test_the_enums_accept_their_own_strings() -> None:
    assert Dispatch("executor") is Dispatch.EXECUTOR
    assert Backpressure("cancel_previous") is Backpressure.CANCEL_PREVIOUS
    assert Dispatch.INLINE == "inline", "a str enum, so a config file can name one"


def test_an_inline_hook_still_runs_on_the_installing_thread(workspace: Path) -> None:
    config = configured()
    threads: list[str] = []

    config.on_reload(
        lambda _previous, _current: threads.append(threading.current_thread().name)
    )

    write(2)
    config.reload()

    assert threads == [threading.current_thread().name]


def test_an_executor_hook_runs_somewhere_else(workspace: Path) -> None:
    config = configured()
    ran = threading.Event()
    threads: list[str] = []

    def record(_previous: Database | None, _current: Database) -> None:
        threads.append(threading.current_thread().name)
        ran.set()

    config.on_reload(record, dispatch=Dispatch.EXECUTOR)

    write(2)
    config.reload()

    assert ran.wait(5)
    assert threads != [threading.current_thread().name]


async def test_a_slow_async_hook_does_not_delay_the_reload(workspace: Path) -> None:
    """The reason the parameter exists: two latencies, not one."""
    config = configured()
    ran = asyncio.Event()

    @config.on_reload_async
    async def slow(_previous: Database | None, _current: Database) -> None:
        await asyncio.sleep(0.3)
        ran.set()

    write(2)
    started = time.perf_counter()
    config.reload()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1, "the install waited for the callback"

    await asyncio.wait_for(ran.wait(), 5)


async def test_a_coroutine_hook_registered_without_a_dispatch_still_runs(
    workspace: Path,
) -> None:
    """Otherwise it is called inline, returns a coroutine, and does nothing."""
    config = configured()
    ran = asyncio.Event()

    @config.on_reload
    async def hook(_previous: Database | None, _current: Database) -> None:
        ran.set()

    write(2)
    config.reload()

    await asyncio.wait_for(ran.wait(), 5)


async def test_latest_keeps_the_newest_install_and_drops_the_middle(
    workspace: Path,
) -> None:
    config = configured()
    seen: list[int] = []

    @config.on_reload_async
    async def slow(_previous: Database | None, current: Database) -> None:
        await asyncio.sleep(0.2)
        seen.append(current.port)

    for port in (2, 3, 4):
        write(port)
        config.reload()
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.8)

    assert seen == [2, 4], "the first ran, the last was kept, the middle dropped"


async def test_serial_drops_nothing(workspace: Path) -> None:
    config = configured()
    seen: list[int] = []

    async def hook(_previous: Database | None, current: Database) -> None:
        await asyncio.sleep(0.05)
        seen.append(current.port)

    config.on_reload_async(hook, backpressure=Backpressure.SERIAL)

    for port in (2, 3, 4):
        write(port)
        config.reload()
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.8)

    assert seen == [2, 3, 4]


async def test_cancel_previous_leaves_only_the_last(workspace: Path) -> None:
    config = configured()
    finished: list[int] = []

    async def hook(_previous: Database | None, current: Database) -> None:
        await asyncio.sleep(0.2)
        finished.append(current.port)

    config.on_reload_async(hook, backpressure=Backpressure.CANCEL_PREVIOUS)

    for port in (2, 3, 4):
        write(port)
        config.reload()
        await asyncio.sleep(0.02)

    await asyncio.sleep(0.6)

    assert finished == [4]


async def test_cancel_previous_needs_tasks_to_cancel(workspace: Path) -> None:
    config = configured()

    with pytest.raises(ValueError, match="cancel_previous"):
        config.on_reload(
            lambda _previous, _current: None,
            dispatch=Dispatch.EXECUTOR,
            backpressure=Backpressure.CANCEL_PREVIOUS,
        )


async def test_an_async_hook_needs_the_loop_it_will_run_on(workspace: Path) -> None:
    config = configured()
    refused: list[str] = []

    async def hook(_previous: Database | None, _current: Database) -> None:
        return None

    def register_off_the_loop() -> None:
        try:
            config.on_reload_async(hook)
        except RuntimeError as error:
            refused.append(str(error))

    thread = threading.Thread(target=register_off_the_loop)
    thread.start()
    thread.join()

    assert refused, "registering without a loop must say so, not run nowhere"
    assert "no running loop" in refused[0]


def test_a_sync_hook_cannot_be_dispatched_to_asyncio(workspace: Path) -> None:
    config = configured()

    with pytest.raises(ValueError, match="needs a coroutine"):
        config.on_reload(lambda _previous, _current: None, dispatch=Dispatch.ASYNCIO)


async def test_a_coroutine_hook_cannot_run_on_a_thread(workspace: Path) -> None:
    config = configured()

    async def hook(_previous: Database | None, _current: Database) -> None:
        return None

    with pytest.raises(ValueError, match="coroutine function"):
        config.on_reload(hook, dispatch=Dispatch.EXECUTOR)


async def test_a_raising_async_hook_does_not_stop_the_others(workspace: Path) -> None:
    config = configured()
    ran: list[str] = []
    reported: list[dict[str, object]] = []

    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: reported.append(context)
    )

    async def raises(_previous: Database | None, _current: Database) -> None:
        raise RuntimeError("hook trouble")

    async def records(_previous: Database | None, _current: Database) -> None:
        ran.append("second")

    config.on_reload_async(raises)
    config.on_reload_async(records)

    write(2)
    config.reload()

    await asyncio.sleep(0.2)

    assert ran == ["second"]
    assert reported, "a raising hook is reported, not swallowed"


async def test_on_change_async_filters_by_path(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    ports: list[int] = []

    @config.on_change_async("port")
    async def hook(_previous: Database | None, current: Database) -> None:
        ports.append(current.port)

    Path("config.toml").write_text('[db]\nhost = "other"\nport = 1\n')
    config.reload()
    await asyncio.sleep(0.15)

    assert ports == [], "the host moved, and the hook watches the port"

    Path("config.toml").write_text('[db]\nhost = "other"\nport = 2\n')
    config.reload()
    await asyncio.sleep(0.15)

    assert ports == [2]
