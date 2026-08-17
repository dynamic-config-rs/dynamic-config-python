"""What the free-threading audit could be turned into a test.

Two kinds of test live here. The ones that run **everywhere** are the
places the audit found correctness riding on the GIL without saying so —
each of them is a latent bug on a GIL build too, so each is asserted on
whichever interpreter happens to be running. The ones guarded by
``FREE_THREADED`` need real parallelism to mean anything, and are skipped
on a build with a GIL rather than passing there for the wrong reason.

The guarded ones are run: CI's `python-free-threaded` job builds the
non-abi3 wheel against CPython 3.14t and runs this file with the rest.
`book/src/python/free-threading.md` is the audit they came out of, and
records what a green suite here still does not prove.
"""

from __future__ import annotations

import os
import sys
import sysconfig
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from dynamic_config import DynamicConfig
from dynamic_config._lifetime import _LIVE_CONFIGS

FREE_THREADED = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))

only_free_threaded = pytest.mark.skipif(
    not FREE_THREADED,
    reason="needs a free-threaded interpreter (python3.13t / python3.14t)",
)


@dataclass
class Database:
    host: str = "h"
    port: int = 1


def write(port: int) -> None:
    Path("config.toml").write_text(f'[db]\nhost = "h"\nport = {port}\n')


# ── Verifiable on either build ─────────────────────────────────────────


def test_a_hook_that_registers_another_hook_does_not_deadlock(
    workspace: Path,
) -> None:
    """The audit's one live question about the hook list.

    The lock guarding `hooks` must not be held while the hooks run: a hook
    that registers, or unregisters, another one takes that same lock, and
    a re-entrant `std::sync::Mutex` is a deadlock rather than a panic. The
    engine clones the list out of the lock before running any of it, which
    is what this asserts — under a deadline, so a regression reads as a
    failure rather than as a suite that never finishes.
    """
    write(1)
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    seen: list[str] = []

    def registers(_old: Database | None, _new: Database) -> None:
        inner = config.on_reload(lambda _o, _n: seen.append("inner"))
        seen.append("registered")
        inner.close()

    config.on_reload(registers)

    done = threading.Event()

    def reload() -> None:
        write(2)
        config.reload()
        done.set()

    worker = threading.Thread(target=reload, daemon=True)
    worker.start()

    assert done.wait(timeout=30), "a hook that registered a hook deadlocked"
    assert seen == ["registered"]


def test_a_hook_that_unregisters_itself_does_not_deadlock(workspace: Path) -> None:
    write(1)
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    ran: list[int] = []
    guard = None

    def once(_old: Database | None, new: Database) -> None:
        ran.append(new.port)
        assert guard is not None
        guard.close()

    guard = config.on_reload(once)

    done = threading.Event()

    def reload() -> None:
        write(2)
        config.reload()
        write(3)
        config.reload()
        done.set()

    worker = threading.Thread(target=reload, daemon=True)
    worker.start()

    assert done.wait(timeout=30), "a hook that removed itself deadlocked"
    assert ran == [2], "the hook removed itself and must not run again"


def test_configurations_built_from_many_threads_are_all_registered(
    workspace: Path,
) -> None:
    """The shutdown registry, mutated concurrently.

    `weakref.WeakSet` is only as atomic as the GIL makes it — its `add` is
    several bytecodes over an internal set plus a pending-removals list —
    and a registry that lost entries would leave watchers running into
    finalization, which is the crash the whole sweep exists to prevent.
    Under a GIL this passes trivially; the lock it is really testing is
    what makes it mean something without one.
    """
    write(1)

    built: list[DynamicConfig[Database]] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def build() -> None:
        start.wait(timeout=30)
        mine = [DynamicConfig(Database, key="db") for _ in range(20)]
        with lock:
            built.extend(mine)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(built) == 160

    live = set(map(id, _LIVE_CONFIGS))

    assert all(id(config) in live for config in built), (
        "a configuration went missing from the shutdown registry"
    )


# ── Only meaningful without a GIL ──────────────────────────────────────


@only_free_threaded
def test_readers_and_reloaders_agree_under_real_parallelism(
    workspace: Path,
) -> None:
    """One install, one generation — with threads that genuinely overlap.

    `test_threading.py` asserts the same properties, and passes under a
    GIL for the wrong reason: the convert-validate-swap step is serialised
    there by the interpreter rather than by the sequence number that is
    supposed to be doing it.
    """
    write(1)
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    stop = threading.Event()
    torn: list[str] = []
    observed: list[list[int]] = [[] for _ in range(4)]

    def read(seen: list[int]) -> None:
        while not stop.is_set():
            model = config.current()
            if model.host != "h" or not isinstance(model.port, int):
                torn.append(repr(model))
            seen.append(config.generation)

    readers = [threading.Thread(target=read, args=(seen,)) for seen in observed]
    for reader in readers:
        reader.start()

    failures: list[BaseException] = []

    def reload_many(base: int) -> None:
        try:
            for step in range(25):
                write(base * 100 + step)
                config.reload()
        except BaseException as failure:
            failures.append(failure)

    writers = [
        threading.Thread(target=reload_many, args=(index,)) for index in range(1, 5)
    ]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(timeout=120)

    stop.set()
    for reader in readers:
        reader.join(timeout=60)

    assert not failures, f"concurrent reloads raised: {failures[:2]}"
    assert not torn, f"a reader saw a half-built model: {torn[:3]}"

    for seen in observed:
        assert seen == sorted(seen), "a reader saw the generation go backwards"


@only_free_threaded
def test_the_module_declares_itself_gil_free() -> None:
    """The extension must not silently re-enable the GIL.

    A module that does not carry `Py_mod_gil = Py_MOD_GIL_NOT_USED` makes a
    free-threaded interpreter turn the GIL back on for the whole process at
    import — which is safe, and gives a free-threaded build none of what it
    was installed for. `sys._is_gil_enabled()` is the observable fact, and
    is asserted rather than the warning that accompanies it: the warning is
    emitted once per process at the first import, so a test that reloads
    the module and watches for it passes whether or not the declaration is
    there. This file's earlier version did exactly that.
    """
    import dynamic_config._core  # noqa: F401  the import is the subject

    if os.environ.get("PYTHON_GIL") == "1" or sys._xoptions.get("gil") == "1":
        pytest.skip("the GIL was forced on from outside; nothing to assert")

    # novermin — `sys._is_gil_enabled` is 3.13+, and the package floor is 3.9.
    # The line is unreachable below 3.13: `only_free_threaded` skips unless
    # `Py_GIL_DISABLED` is set, which no interpreter before 3.13 can report.
    # vermin reads statically and cannot see that, so the exemption is here
    # rather than on the whole file.
    assert not sys._is_gil_enabled(), (  # novermin
        "importing the extension re-enabled the GIL: the module is missing "
        "Py_MOD_GIL_NOT_USED (`#[pymodule(gil_used = false)]`)"
    )


def test_a_notifier_resolves_waiters_registered_from_many_threads(
    workspace: Path,
) -> None:
    """The notifier's waiter list is mutated from two directions at once.

    Registrations arrive on whichever thread runs the awaiting loop; the
    resolution happens on the notifier's own. Under a GIL the list
    operations are atomic by accident — the lock inside `Notifier` is what
    has to make them atomic on purpose.
    """
    import asyncio

    write(1)
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    resolved: list[int] = []
    ready = threading.Barrier(5)
    failures: list[BaseException] = []

    def wait_on_its_own_loop() -> None:
        async def wait() -> None:
            ready.wait(timeout=30)
            model = await config.changed_async(timeout=30)

            if model is not None:
                resolved.append(model.port)

        try:
            asyncio.run(wait())
        except BaseException as failure:
            failures.append(failure)

    waiting = [threading.Thread(target=wait_on_its_own_loop) for _ in range(4)]

    for thread in waiting:
        thread.start()

    ready.wait(timeout=30)

    # Every waiter is registered by now, on four different loops.
    for _ in range(20):
        write(2)
        config.reload()

        if all(not thread.is_alive() for thread in waiting):
            break

        import time

        time.sleep(0.05)

    for thread in waiting:
        thread.join(timeout=30)

    assert not failures, f"a waiter raised: {failures[:2]}"
    assert len(resolved) == 4, f"only {len(resolved)} of four waiters woke"


@only_free_threaded
def test_a_group_reloads_atomically_while_readers_run(workspace: Path) -> None:
    """Prepare-then-commit, with real readers watching for a mixed state."""
    from dynamic_config import ConfigGroup

    Path("config.toml").write_text(
        '[db]\nhost = "h"\nport = 1\n[cache]\nhost = "h"\nport = 1\n'
    )

    database = DynamicConfig(Database, key="db").file("config.toml")
    cache = DynamicConfig(Database, key="cache").file("config.toml")
    group = ConfigGroup(database, cache)
    group.init()

    stop = threading.Event()
    mixed: list[tuple[int, int]] = []

    def read() -> None:
        while not stop.is_set():
            pair = (database.current().port, cache.current().port)

            if pair[0] != pair[1]:
                mixed.append(pair)

    readers = [threading.Thread(target=read) for _ in range(3)]

    for reader in readers:
        reader.start()

    for port in range(2, 40):
        Path("config.toml").write_text(
            f'[db]\nhost = "h"\nport = {port}\n[cache]\nhost = "h"\nport = {port}\n'
        )
        group.reload_atomic()

    stop.set()

    for reader in readers:
        reader.join(timeout=60)

    # The commits are still two stores, so a reader between them sees the
    # older half — what atomicity buys is that no *refusal* can leave the
    # two apart for good, which the other tests assert. This one only has
    # to show the window is not a tear.
    assert all(abs(first - second) <= 1 for first, second in mixed), mixed[:3]
