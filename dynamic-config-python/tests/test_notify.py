"""The event-loop bridge: one thread per configuration, and no polling."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import DynamicConfig, InvalidError, Reloaded, ReloadFailed


class Database(BaseModel):
    host: str
    port: int


def write(port: int, name: str = "config.toml") -> None:
    Path(name).write_text(f'[db]\nhost = "h"\nport = {port}\n')


def broken(name: str = "config.toml") -> None:
    Path(name).write_text('[db]\nhost = "h"\nport = "not a number"\n')


def configured(name: str = "config.toml", key: str = "db") -> DynamicConfig[Database]:
    Path(name).write_text(f'[{key}]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key=key).file(name)
    config.init()

    return config


def notifier_threads() -> list[str]:
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("dynamic-config-notify-")
    ]


async def test_a_wait_parks_one_named_thread_per_configuration(
    workspace: Path,
) -> None:
    config = configured("only.toml", key="only")
    before = notifier_threads()

    assert "dynamic-config-notify-only" not in before, (
        "nothing is started until something awaits"
    )

    waiters = [asyncio.create_task(config.changed_async()) for _ in range(5)]
    await asyncio.sleep(0.1)

    started = [name for name in notifier_threads() if name not in before]

    assert started == ["dynamic-config-notify-only"], (
        "five waiters, one thread — and it says which configuration it is for"
    )

    for waiter in waiters:
        waiter.cancel()

    await asyncio.gather(*waiters, return_exceptions=True)


async def test_two_configurations_park_two_threads(workspace: Path) -> None:
    Path("second.toml").write_text('[second]\nhost = "h"\nport = 2\n')

    first = configured("first.toml", key="first")
    second = DynamicConfig(Database, key="second").file("second.toml")
    second.init()
    before = notifier_threads()

    waiting = [
        asyncio.create_task(first.changed_async()),
        asyncio.create_task(second.changed_async()),
    ]
    await asyncio.sleep(0.1)

    started = sorted(name for name in notifier_threads() if name not in before)

    assert started == [
        "dynamic-config-notify-first",
        "dynamic-config-notify-second",
    ]

    for waiter in waiting:
        waiter.cancel()

    await asyncio.gather(*waiting, return_exceptions=True)


async def test_cancelling_a_wait_is_noticed_at_once(workspace: Path) -> None:
    """The number this design exists for: not a quarter of a second."""
    config = configured()

    waiter = asyncio.create_task(config.changed_async())
    await asyncio.sleep(0.05)

    started = time.perf_counter()
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    elapsed = time.perf_counter() - started

    assert elapsed < 0.05, f"cancellation took {elapsed * 1000:.0f} ms"


async def test_every_waiter_is_resolved_by_one_install(workspace: Path) -> None:
    config = configured()

    waiters = [asyncio.create_task(config.changed_async(timeout=5)) for _ in range(4)]
    await asyncio.sleep(0.05)

    write(2)
    config.reload()

    models = await asyncio.gather(*waiters)

    assert [model.port for model in models if model is not None] == [2, 2, 2, 2]


async def test_an_install_racing_the_registration_is_not_missed(
    workspace: Path,
) -> None:
    """The check-register-check, exercised from the side that races it."""
    config = configured()

    def reload_to(port: int) -> None:
        write(port)
        config.reload()

    for round_number in range(2, 30):
        # Started first, so the install lands in the window between the
        # wait's two reads of the generation — the one a lost wake-up
        # would fall through.
        reloading = threading.Thread(target=reload_to, args=(round_number,))
        reloading.start()

        model = await asyncio.wait_for(config.changed_async(timeout=5), 10)
        reloading.join()

        assert model is not None, f"round {round_number} never woke"


async def test_an_install_is_reported_once(workspace: Path) -> None:
    """One install, one turn of the iterator — never two."""
    config = configured()
    seen: list[int] = []

    async def consume() -> None:
        async for model in config.changes():
            seen.append(model.port)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    for port in (2, 3, 4):
        write(port)
        config.reload()
        await asyncio.sleep(0.1)

    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    assert seen == [2, 3, 4]


async def test_a_wait_ends_when_the_configuration_is_released(
    workspace: Path,
) -> None:
    config = configured()

    waiter = asyncio.create_task(config.changed_async())
    await asyncio.sleep(0.05)

    config._core.release()

    assert await asyncio.wait_for(waiter, 5) is None, "release ends the wait"


async def test_events_reports_installs(workspace: Path) -> None:
    config = configured()
    events: list[object] = []

    async def consume() -> None:
        async for event in config.events():
            events.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    write(2)
    config.reload()
    await asyncio.sleep(0.15)

    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    assert len(events) == 1

    event = events[0]

    assert isinstance(event, Reloaded)
    assert event.generation == 2
    assert event.changed == ("port",)
    assert event.at > 0


async def test_events_reports_a_refusal_natively(
    workspace: Path,
) -> None:
    """No poll interval: the refusal itself wakes the stream."""
    config = configured()
    events: list[object] = []

    async def consume() -> None:
        async for event in config.events():
            events.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    write(2)
    config.reload()
    await asyncio.sleep(0.15)

    broken()

    with pytest.raises(InvalidError):
        config.reload()

    await asyncio.sleep(0.25)

    write(3)
    config.reload()
    await asyncio.sleep(0.15)

    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    kinds = [type(event).__name__ for event in events]

    assert kinds == ["Reloaded", "ReloadFailed", "Reloaded"]

    failure = events[1]

    assert isinstance(failure, ReloadFailed)
    assert failure.kind == "invalid"
    assert failure.consecutive == 1


async def test_events_failure_poll_is_accepted_and_warns(
    workspace: Path,
) -> None:
    """The 0.3.0 parameter still parses; it just no longer does anything."""
    config = configured()
    write(2)
    config.reload()

    received: list[object] = []

    with pytest.warns(DeprecationWarning, match="failure_poll is ignored"):
        stream = config.events(failure_poll=1.0)

    async def consume() -> None:
        async for event in stream:
            received.append(event)
            break

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.1)

    write(3)
    config.reload()
    await asyncio.wait_for(consumer, timeout=5)

    assert len(received) == 1
    assert isinstance(received[0], Reloaded)


async def test_no_event_carries_a_value(workspace: Path) -> None:
    """The rule every diagnostic here follows: paths, never values."""
    config = configured()
    events: list[object] = []

    async def consume() -> None:
        async for event in config.events():
            events.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    write(4242)
    config.reload()
    await asyncio.sleep(0.15)

    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    assert "4242" not in repr(events)


async def test_waiting_does_not_use_the_configuration_executor(
    workspace: Path,
) -> None:
    """A wait is a parking spot; it must not occupy a pool sized for work."""
    from concurrent.futures import ThreadPoolExecutor

    submitted = 0

    class Counting(ThreadPoolExecutor):
        def submit(self, *arguments: object, **keywords: object):  # type: ignore[override,no-untyped-def]
            nonlocal submitted
            submitted += 1

            return super().submit(*arguments, **keywords)  # type: ignore[arg-type]

    with Counting(1) as pool:
        write(1)

        config = DynamicConfig(Database, key="db", executor=pool).file("config.toml")
        config.init()

        waiter = asyncio.create_task(config.changed_async())
        await asyncio.sleep(0.2)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)

    assert submitted == 0
