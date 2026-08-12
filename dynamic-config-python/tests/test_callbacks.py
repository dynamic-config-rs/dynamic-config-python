"""Reacting to a reload: the hook surface, and what it promises.

`test_lifecycle.py` covers that hooks fire at all. This file is about the
contract a caller writes against — what the arguments mean, what a read
inside a hook sees, what happens to the ones after a hook that raises,
and the two ergonomic shapes (`on_reload` as a decorator, `on_change` as
a filter).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from dynamic_config import DynamicConfig


class Pool(BaseModel):
    max_size: int = Field(default=8, ge=1)


class Service(BaseModel):
    host: str = "localhost"
    port: int = 1
    pool: Pool = Pool()


def write(*, host: str = "localhost", port: int = 1, max_size: int = 8) -> None:
    Path("config.toml").write_text(
        f'[svc]\nhost = "{host}"\nport = {port}\n\n[svc.pool]\nmax_size = {max_size}\n'
    )


def loaded() -> DynamicConfig[Service]:
    config = DynamicConfig(Service, key="svc").file("config.toml")
    config.init()

    return config


# ── What the arguments mean ────────────────────────────────────────────


def test_the_first_install_has_no_previous_model(workspace: Path) -> None:
    """`old is None` is how a hook tells startup from a change."""
    write()

    config = DynamicConfig(Service, key="svc").file("config.toml")
    seen: list[tuple[int | None, int]] = []

    config.on_reload(
        lambda old, new: seen.append((old.port if old else None, new.port))
    )

    config.init()
    write(port=2)
    config.reload()

    assert seen == [(None, 1), (1, 2)]


def test_a_read_inside_a_hook_sees_the_new_model(workspace: Path) -> None:
    """The publish hook is registered first, so `current()` does not lag."""
    write()
    config = loaded()

    agreed: list[bool] = []
    config.on_reload(lambda _old, new: agreed.append(config.current() is new))

    write(port=2)
    config.reload()

    assert agreed == [True], "current() lagged the model the hook was handed"


def test_hooks_run_in_registration_order(workspace: Path) -> None:
    write()
    config = loaded()
    order: list[str] = []

    config.on_reload(lambda _old, _new: order.append("first"))
    config.on_reload(lambda _old, _new: order.append("second"))

    write(port=2)
    config.reload()

    assert order == ["first", "second"]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_a_raising_hook_does_not_stop_the_ones_after_it(workspace: Path) -> None:
    write()
    config = loaded()
    reached: list[str] = []

    def explodes(_old: Service | None, _new: Service) -> None:
        raise RuntimeError("this hook is broken")

    config.on_reload(explodes)
    config.on_reload(lambda _old, _new: reached.append("after"))

    write(port=2)
    config.reload()

    assert reached == ["after"]
    assert config.current().port == 2, "and the install itself still happened"


def test_a_hook_runs_on_the_thread_that_reloaded(workspace: Path) -> None:
    write()
    config = loaded()
    threads: list[str] = []

    config.on_reload(lambda _old, _new: threads.append(threading.current_thread().name))

    write(port=2)
    config.reload()

    worker = threading.Thread(target=config.reload, name="reloader")
    write(port=3)
    worker.start()
    worker.join(timeout=10)

    assert threads[0] == threading.main_thread().name
    assert threads[1] == "reloader", "a hook runs where the reload did"


# ── The guard ──────────────────────────────────────────────────────────


def test_a_closed_guard_stops_the_hook(workspace: Path) -> None:
    write()
    config = loaded()
    calls: list[int] = []

    guard = config.on_reload(lambda _old, new: calls.append(new.port))

    write(port=2)
    config.reload()
    guard.close()
    write(port=3)
    config.reload()

    assert calls == [2]

    guard.close()  # idempotent


def test_a_guard_unregisters_at_the_end_of_a_with(workspace: Path) -> None:
    write()
    config = loaded()
    calls: list[int] = []

    with config.on_reload(lambda _old, new: calls.append(new.port)):
        write(port=2)
        config.reload()

    write(port=3)
    config.reload()

    assert calls == [2]


def test_on_reload_works_as_a_decorator_and_keeps_the_function(
    workspace: Path,
) -> None:
    """The guard forwards calls, so the decorated name is still callable."""
    write()
    config = loaded()
    calls: list[int] = []

    @config.on_reload
    def record(_old: Service | None, new: Service) -> None:
        calls.append(new.port)

    write(port=2)
    config.reload()

    assert calls == [2]

    # Still a function: called directly, it does what it says.
    record(None, config.current())
    assert calls == [2, 2]

    assert record.hook is not None
    assert record.hook.__name__ == "record"

    record.close()
    write(port=3)
    config.reload()

    assert calls == [2, 2], "closing the guard unregistered it"


# ── The filter ─────────────────────────────────────────────────────────


def test_on_change_fires_only_for_the_named_path(workspace: Path) -> None:
    write()
    config = loaded()
    resized: list[int] = []

    @config.on_change("pool.max_size")
    def resize(_old: Service | None, new: Service) -> None:
        resized.append(new.pool.max_size)

    write(host="elsewhere")
    config.reload()
    assert resized == [], "a neighbouring field moved, not this one"

    write(host="elsewhere", max_size=32)
    config.reload()
    assert resized == [32]

    write(host="elsewhere", port=9, max_size=32)
    config.reload()
    assert resized == [32], "the path did not move again"

    resize.close()


def test_on_change_accepts_several_paths(workspace: Path) -> None:
    write()
    config = loaded()
    calls: list[str] = []

    config.on_change("host", "port")(lambda _old, _new: calls.append("fired"))

    write(max_size=16)
    config.reload()
    assert calls == []

    write(max_size=16, port=2)
    config.reload()
    assert calls == ["fired"]

    write(max_size=16, port=2, host="elsewhere")
    config.reload()
    assert calls == ["fired", "fired"]


def test_a_table_covers_what_is_inside_it(workspace: Path) -> None:
    """Naming a section means the section."""
    write()
    config = loaded()
    calls: list[int] = []

    config.on_change("pool")(lambda _old, new: calls.append(new.pool.max_size))

    write(port=2)
    config.reload()
    assert calls == []

    write(port=2, max_size=64)
    config.reload()
    assert calls == [64]


def test_on_change_runs_on_the_first_install(workspace: Path) -> None:
    """A hook that sets something up should not wait for an edit."""
    write(max_size=16)

    config = DynamicConfig(Service, key="svc").file("config.toml")
    sizes: list[int] = []

    config.on_change("pool.max_size")(lambda _old, new: sizes.append(new.pool.max_size))
    config.init()

    assert sizes == [16], "nothing to compare against is not nothing to do"


def test_on_change_needs_a_path(workspace: Path) -> None:
    write()
    config = loaded()

    with pytest.raises(ValueError, match="at least one path"):
        config.on_change()


def test_on_change_never_reports_a_value(workspace: Path) -> None:
    """The filter compares; it does not hand anything to a log."""
    write()
    config = loaded()
    guard = config.on_change("host")(lambda _old, _new: None)

    assert "localhost" not in repr(guard)

    guard.close()


def test_a_hook_may_reload_nothing_and_read_everything(workspace: Path) -> None:
    """A hook that asks the configuration questions does not deadlock."""
    write()
    config = loaded()
    answers: list[object] = []

    @config.on_reload
    def inspect(_old: Service | None, _new: Service) -> None:
        answers.append(config.source_of("port"))
        answers.append(config.generation)

    write(port=2)
    config.reload()

    assert len(answers) == 2
    assert answers[1] == 2, "the generation is already bumped when a hook runs"

    inspect.close()


def test_a_hook_that_captures_the_configuration_is_still_collectable(
    workspace: Path,
) -> None:
    """The documented idiom must not leak the configuration.

    `@config.on_reload` closures that read `config.current()` close a
    cycle through the Rust `Config`, and a `#[pyclass]` with no
    `tp_traverse` is a wall the collector stops at — so every
    configuration built that way lived until the process exited, models,
    hooks and leaked layers included. `Config.__traverse__` is what lets
    the collector see the edge.
    """
    import gc
    import weakref

    def build(capture: bool) -> weakref.ref[DynamicConfig[Service]]:
        write()
        config = loaded()

        if capture:
            config.on_reload(lambda _old, _new: config.current())
        else:
            config.on_reload(lambda _old, _new: None)

        return weakref.ref(config)

    captured = [build(True) for _ in range(3)]
    plain = [build(False) for _ in range(3)]

    gc.collect()
    gc.collect()

    assert [reference() for reference in captured] == [None, None, None], (
        "a hook capturing its own configuration kept it alive"
    )
    assert [reference() for reference in plain] == [None, None, None]
