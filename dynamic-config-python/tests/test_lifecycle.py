"""Reload, watch, hooks, changes — and what a refusal leaves behind."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, field_validator

from dynamic_config import DynamicConfig, InvalidError, ParseError, Watch


class Database(BaseModel):
    host: str
    port: int = 5432


class Checked(BaseModel):
    port: int

    @field_validator("port")
    @classmethod
    def _not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("port 0 binds to a random port, which nobody means")

        return value


def write(path: str, port: int, host: str = "h") -> None:
    Path(path).write_text(f'[db]\nhost = "{host}"\nport = {port}\n')


def settles_on(read, expected, *, edit=None, seconds: float = 15.0) -> bool:
    """Waits for a watcher, rewriting when asked.

    A poll watcher takes its baseline on its first tick, so a single write
    can land inside it — the same honest shape the Rust suite pins.
    """
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        if edit is not None:
            edit()
        time.sleep(0.2)

        if read() == expected:
            return True

    return False


def test_reload_installs_and_bumps_the_generation(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    assert config.generation == 1

    write("config.toml", 2)
    config.reload()

    assert config.current().port == 2
    assert config.generation == 2


def test_the_repr_carries_the_shape_and_the_generation(workspace: Path) -> None:
    write("config.toml", 5432, host="a-value-nobody-should-see")

    config = DynamicConfig(Database, key="db").file("config.toml")

    assert repr(config) == "<DynamicConfig Database key='db' generation=0>", (
        "generation zero is how a debugger sees 'nothing installed yet'"
    )

    config.init()
    assert repr(config) == "<DynamicConfig Database key='db' generation=1>"

    write("config.toml", 5433, host="a-value-nobody-should-see")
    config.reload()

    assert repr(config) == "<DynamicConfig Database key='db' generation=2>"
    assert "a-value-nobody-should-see" not in repr(config), "shape, never values"
    assert "5433" not in repr(config)


def test_a_rejected_reload_keeps_the_previous_model(workspace: Path) -> None:
    Path("config.toml").write_text("[db]\nport = 1\n")

    config = DynamicConfig(Checked, key="db").file("config.toml")
    config.init()
    good = config.current()

    Path("config.toml").write_text("[db]\nport = 0\n")

    with pytest.raises(InvalidError) as failure:
        config.reload()

    assert config.current() is good, "the previous model keeps serving"
    assert config.generation == 1, "a refused reload does not bump the generation"
    assert "port" in str(failure.value)


def test_a_validation_failure_carries_scrubbed_reports(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nport = "not-a-number"\n')

    config = DynamicConfig(Checked, key="db").file("config.toml")

    with pytest.raises(InvalidError) as failure:
        config.init()

    reports = getattr(failure.value, "errors", None)
    assert reports, "Pydantic's own report survives the boundary"

    for report in reports:
        assert "loc" in report
        assert "msg" in report
        assert "input" not in report, "the offending value does not travel"

    assert "not-a-number" not in str(failure.value)


def test_a_malformed_file_is_a_parse_failure_that_changes_nothing(
    workspace: Path,
) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    good = config.current()

    Path("config.toml").write_text("[db\nnot toml at all")

    with pytest.raises(ParseError):
        config.reload()

    assert config.current() is good


def test_hooks_fire_with_both_models_and_a_guard_unregisters(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    seen: list[tuple[int | None, int]] = []
    guard = config.on_reload(
        lambda old, new: seen.append((None if old is None else old.port, new.port))
    )

    write("config.toml", 2)
    config.reload()
    assert seen == [(1, 2)]

    guard.close()
    write("config.toml", 3)
    config.reload()
    assert seen == [(1, 2)], "a closed guard stops the hook"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_raising_hook_does_not_stop_the_others(workspace: Path) -> None:
    """A raising hook does not stop the hooks after it.

    The raise is *reported*, through Python's unraisable channel — which
    is what the filter above is acknowledging rather than hiding.
    """
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    ran: list[str] = []

    def explodes(_old, _new):
        ran.append("first")
        raise RuntimeError("this hook is broken")

    config.on_reload(explodes)
    config.on_reload(lambda _old, _new: ran.append("second"))

    write("config.toml", 2)
    config.reload()

    assert ran == ["first", "second"], "the second hook still runs"
    assert config.current().port == 2, "and the install stands"


def test_a_hook_may_call_back_into_the_configuration(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    observed: list[int] = []
    config.on_reload(lambda _old, _new: observed.append(config.current().port))

    write("config.toml", 2)
    config.reload()

    assert observed == [2], "the new model is already published when hooks run"


def test_replace_installs_a_model_the_caller_built(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    seen: list[int] = []
    config.on_reload(lambda _old, new: seen.append(new.port))
    config.replace(Database(host="hand", port=99))

    assert config.current().port == 99
    assert seen == [99]

    with pytest.raises(TypeError):
        config.replace("not a model")  # type: ignore[arg-type]


def test_the_watcher_reloads_on_an_edit(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    with config.watch(debounce=0.05, poll_interval=0.05) as watch:
        assert watch.running
        assert settles_on(
            lambda: config.current().port,
            11,
            edit=lambda: write("config.toml", 11),
        )

    assert not watch.running, "leaving the block stops the watcher"


def test_two_configurations_watch_side_by_side(workspace: Path) -> None:
    write("a.toml", 1)
    write("b.toml", 2)

    first = DynamicConfig(Database, key="db").file("a.toml")
    second = DynamicConfig(Database, key="db").file("b.toml")
    first.init()
    second.init()

    with (
        first.watch(debounce=0.05, poll_interval=0.05),
        second.watch(debounce=0.05, poll_interval=0.05),
    ):
        assert settles_on(
            lambda: first.current().port, 11, edit=lambda: write("a.toml", 11)
        )
        assert second.current().port == 2, "the other instance is untouched"


def test_a_watcher_ignores_a_reload_it_cannot_validate(workspace: Path) -> None:
    Path("config.toml").write_text("[db]\nport = 1\n")

    config = DynamicConfig(Checked, key="db").file("config.toml")
    config.init()
    good = config.current()

    with config.watch(debounce=0.05, poll_interval=0.05):
        Path("config.toml").write_text("[db]\nport = 0\n")
        time.sleep(1.0)

        assert config.current() is good, "a refused watch reload changes nothing"
        assert config.generation == 1


def test_changed_blocks_until_the_next_install(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    assert config.changed(timeout=0.05) is None, "nothing installed yet"

    import threading

    def reload_soon() -> None:
        time.sleep(0.2)
        write("config.toml", 2)
        config.reload()

    thread = threading.Thread(target=reload_soon)
    thread.start()

    model = config.changed(timeout=10.0)
    thread.join()

    assert model is not None
    assert model.port == 2


async def test_changes_yields_every_install(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    async def reload_twice() -> None:
        for port in (2, 3):
            await asyncio.sleep(0.1)
            write("config.toml", port)
            await asyncio.get_running_loop().run_in_executor(None, config.reload)

    task = asyncio.create_task(reload_twice())
    seen: list[int] = []

    async for model in config.changes():
        seen.append(model.port)
        if len(seen) == 2:
            break

    await task
    assert seen == [2, 3]


async def test_changes_can_be_cancelled_mid_await(workspace: Path) -> None:
    write("config.toml", 1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    async def consume() -> None:
        async for _model in config.changes():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The engine is unharmed: a reload still works afterwards.
    write("config.toml", 2)
    config.reload()
    assert config.current().port == 2


def test_init_and_current_is_the_two_calls_that_always_pair(workspace: Path) -> None:
    write("config.toml", 1)

    db = DynamicConfig(Database, key="db").file("config.toml").init_and_current()

    assert db.port == 1
    assert isinstance(db, Database)


def test_init_and_current_raises_from_the_load_rather_than_the_read(
    workspace: Path,
) -> None:
    """A failure comes back as the load's, with nothing half-installed."""
    Path("config.toml").write_text('[db]\nport = "not-a-number"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")

    with pytest.raises(InvalidError):
        config.init_and_current()

    assert config.try_current() is None
    assert config.generation == 0


async def test_init_and_current_async_is_the_same_pair_awaited(
    workspace: Path,
) -> None:
    write("config.toml", 7)

    config = DynamicConfig(Database, key="db").file("config.toml")
    db = await config.init_and_current_async()

    assert db.port == 7
    assert config.generation == 1


# ── The lifetime as a block ────────────────────────────────────────────


def test_watching_stops_the_watcher_even_when_the_block_raises(
    workspace: Path,
) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    escaped: Watch | None = None

    def fail_inside_the_block() -> None:
        nonlocal escaped

        with config.watching(debounce=0.05) as watch:
            escaped = watch

            assert watch.running

            raise ZeroDivisionError("whatever the block was doing")

    with pytest.raises(ZeroDivisionError):
        fail_inside_the_block()

    assert escaped is not None
    assert not escaped.running


def test_running_loads_watches_and_stops(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")

    with config.running(debounce=0.05) as model:
        assert model.port == 1
        assert config.current().port == 1

        Path("config.toml").write_text('[db]\nhost = "h"\nport = 2\n')

        for _ in range(100):
            if config.generation > 1:
                break

            time.sleep(0.05)

        assert config.current().port == 2, "the block watches, as it says it does"

    assert config.generation > 1


def test_running_without_a_watcher_only_loads(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")

    with config.running(watch=False) as model:
        assert model.port == 1

        Path("config.toml").write_text('[db]\nhost = "h"\nport = 2\n')
        time.sleep(0.2)

        assert config.current().port == 1, "nothing is watching, so nothing reloads"


async def test_running_async_is_the_lifespan_shape(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")

    async with config.running_async(debounce=0.05) as model:
        assert model.port == 1

    async with config.watching_async(debounce=0.05) as watch:
        assert watch.running

    assert not watch.running
