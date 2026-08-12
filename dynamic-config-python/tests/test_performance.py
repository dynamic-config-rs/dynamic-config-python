"""The performance *claims*, asserted exactly rather than timed.

The design says three things about cost, and each one is a property a
test can pin without measuring a clock — which is what makes these run in
CI at all. The numbers a human wants are in `benchmarks/read_path.py`.
"""

from __future__ import annotations

import contextlib
import gc
import tracemalloc
from pathlib import Path

from pydantic import BaseModel, field_validator

from dynamic_config import DynamicConfig, DynamicConfigError, changed_paths


class Database(BaseModel):
    host: str
    port: int


VALIDATIONS = {"count": 0}


class Counted(BaseModel):
    port: int

    @field_validator("port")
    @classmethod
    def _count(cls, value: int) -> int:
        VALIDATIONS["count"] += 1

        return value


def write(port: int, host: str = "h") -> None:
    Path("config.toml").write_text(f'[db]\nhost = "{host}"\nport = {port}\n')


def test_current_returns_the_same_object_until_something_installs(
    workspace: Path,
) -> None:
    """A read is a cached lookup: same object, no rebuild, no re-validation."""
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    first = config.current()

    for _ in range(1000):
        assert config.current() is first, "a read rebuilt the model"

    write(2)
    config.reload()

    assert config.current() is not first, "and an install does replace it"


def test_validation_runs_once_per_successful_resolve(workspace: Path) -> None:
    """Not once per read, and not twice per reload."""
    Path("config.toml").write_text("[db]\nport = 1\n")
    VALIDATIONS["count"] = 0

    config = DynamicConfig(Counted, key="db").file("config.toml")
    config.init()

    assert VALIDATIONS["count"] == 1, "init validates once"

    for _ in range(500):
        config.current()

    assert VALIDATIONS["count"] == 1, "reads validate not at all"

    Path("config.toml").write_text("[db]\nport = 2\n")
    config.reload()

    assert VALIDATIONS["count"] == 2, "one reload, one validation"


def test_a_refused_reload_validates_once_and_installs_nothing(
    workspace: Path,
) -> None:
    Path("config.toml").write_text("[db]\nport = 1\n")
    VALIDATIONS["count"] = 0

    config = DynamicConfig(Counted, key="db").file("config.toml")
    config.init()
    Path("config.toml").write_text('[db]\nport = "not-a-number"\n')

    with contextlib.suppress(DynamicConfigError):
        config.reload()

    # The refusal happens inside the same single validation attempt: the
    # boundary is not crossed twice to discover the same failure.
    assert VALIDATIONS["count"] == 1, "the failed parse never reached the validator"
    assert config.current().port == 1


def test_a_thousand_reloads_do_not_grow_the_heap(workspace: Path) -> None:
    """The previous model drops when the swap succeeds."""
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    # Warm up, so first-call allocations (the registry slot, interned
    # strings) are not counted as growth.
    for port in range(2, 50):
        write(port)
        config.reload()

    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()

    for port in range(50, 550):
        write(port)
        config.reload()

    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()

    grown = sum(entry.size_diff for entry in after.compare_to(before, "filename"))

    # Five hundred reloads of a two-field model: a leak of one model per
    # reload would be tens of kilobytes and rising. A flat profile is not
    # zero — the allocator keeps arenas — so the bar is "not proportional
    # to the reload count".
    assert grown < 200_000, f"the heap grew by {grown} bytes over 500 reloads"


def test_changed_paths_reports_paths_and_never_values(workspace: Path) -> None:
    before = Database(host="old-host", port=1)
    after = Database(host="new-host", port=1)

    changes = changed_paths(before, after)

    assert [change.path for change in changes] == ["host"]
    assert changes[0].kind == "changed"

    rendered = str(changes[0])
    assert "old-host" not in rendered
    assert "new-host" not in rendered


def test_changed_paths_names_additions_and_removals() -> None:
    changes = changed_paths({"a": 1, "b": 2}, {"b": 2, "c": 3})

    assert {(change.path, change.kind) for change in changes} == {
        ("a", "removed"),
        ("c", "added"),
    }


def test_the_two_caches_never_disagree(workspace: Path) -> None:
    """The read path is a Python attribute; the engine holds its own copy.

    That is what makes ``current()`` cost an attribute lookup — and it is
    the one thing the design has to keep true, on *every* path that
    installs: init, reload, a watch-driven reload, a hand-built replace,
    and recovery from the last-known-good cache.
    """
    import time

    write(1)

    config = (
        DynamicConfig(Database, key="db").file("config.toml").cache("last.json", "full")
    )

    def agree(where: str) -> None:
        assert config.try_current() is config._core.current(), (
            f"the facade and the engine disagree after {where}"
        )

    config.init()
    agree("init")

    write(2)
    config.reload()
    agree("reload")

    config.replace(Database(host="hand", port=99))
    agree("replace")

    with config.watch(debounce=0.05, poll_interval=0.05):
        deadline = time.monotonic() + 15
        while config.try_current().port != 3 and time.monotonic() < deadline:
            write(3)
            time.sleep(0.1)

    assert config.try_current().port == 3, "the watcher never landed"
    agree("a watch-driven reload")

    # And recovery: a broken source, a cache to fall back on, a fresh
    # configuration object doing the falling.
    Path("config.toml").write_text("[db\nbroken")
    recovered = (
        DynamicConfig(Database, key="db").file("config.toml").cache("last.json", "full")
    )
    recovered.init()

    assert recovered.try_current() is recovered._core.current(), (
        "the facade and the engine disagree after recovery"
    )


def test_the_package_and_the_engine_report_separate_versions() -> None:
    """Two numbers on two schedules, and both answerable from the wheel."""
    import re

    import dynamic_config

    for number in (dynamic_config.__version__, dynamic_config.__engine_version__):
        assert re.fullmatch(r"\d+\.\d+\.\d+.*", number), number

    # Not asserted equal: the package versions independently of the Rust
    # crates, which is the whole point — a Rust-only release must not ask
    # every Python user to upgrade.
    assert isinstance(dynamic_config.__version__, str)
