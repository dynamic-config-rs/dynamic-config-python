"""Readers under a reload storm, and the GIL rules around them."""

from __future__ import annotations

import gc
import threading
from pathlib import Path

from pydantic import BaseModel

from dynamic_config import DynamicConfig


class Database(BaseModel):
    host: str
    port: int


def write(port: int) -> None:
    Path("config.toml").write_text(f'[db]\nhost = "h"\nport = {port}\n')


def test_readers_never_see_a_torn_model(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    stop = threading.Event()
    torn: list[str] = []

    def read(seen: list[int]) -> None:
        while not stop.is_set():
            model = config.current()
            # Every published model is fully built: host and port come
            # from the same resolve, never from two.
            if model.host != "h" or not isinstance(model.port, int):
                torn.append(repr(model))
            seen.append(config.generation)

    # One list per reader: the property is that *an observer* never sees
    # time run backwards. Interleaving four threads' appends into one list
    # would only measure the scheduler.
    observed: list[list[int]] = [[] for _ in range(4)]
    readers = [threading.Thread(target=read, args=(seen,)) for seen in observed]
    for reader in readers:
        reader.start()

    for port in range(2, 40):
        write(port)
        config.reload()

    stop.set()
    for reader in readers:
        reader.join()

    assert not torn, f"a reader saw a half-built model: {torn[:3]}"
    assert config.current().port == 39

    for seen in observed:
        assert seen == sorted(seen), "a reader saw the generation go backwards"


def test_a_reload_from_two_threads_serialises(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    failures: list[BaseException] = []

    def reload_many() -> None:
        try:
            for _ in range(25):
                config.reload()
        except BaseException as failure:  # pragma: no cover - the assertion below
            failures.append(failure)

    threads = [threading.Thread(target=reload_many) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"concurrent reloads raised: {failures[:2]}"

    # Not an exact count: two reloads that resolve the same tree at the
    # same time publish it once, which is the better answer — one logical
    # change, one wake-up. What has to hold is that the generation moved,
    # never backwards, and the final model is the real one.
    assert config.generation > 1
    assert config.current().port == 1


def test_a_watcher_and_a_manual_reload_can_race(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    with config.watch(debounce=0.05, poll_interval=0.05):
        for port in range(2, 12):
            write(port)
            config.reload()

    assert config.current().port == 11
    assert config.try_current() is not None


def test_models_do_not_accumulate_across_reloads(workspace: Path) -> None:
    write(1)

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    gc.collect()
    before = len([o for o in gc.get_objects() if isinstance(o, Database)])

    for port in range(2, 60):
        write(port)
        config.reload()

    gc.collect()
    after = len([o for o in gc.get_objects() if isinstance(o, Database)])

    # One live model (plus whatever a caller happens to hold): the
    # previous one drops when the swap succeeds and the last reader lets
    # go. A leak would show as ~60.
    assert after - before <= 2, f"models accumulated: {before} → {after}"
