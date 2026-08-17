"""Which pool pays for the blocking half, and who owns it."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel

import dynamic_config
from dynamic_config import (
    DynamicConfig,
    configure_executor,
    executor,
    set_executor,
)
from dynamic_config._executor import default_executor


class Database(BaseModel):
    host: str
    port: int


@pytest.fixture(autouse=True)
def _restore_the_default() -> object:
    """No test leaves its pool behind for the next one."""
    yield None

    set_executor(None)


async def test_the_configured_pool_runs_the_blocking_half(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = 1\n')
    threads: set[str] = set()

    class Naming(ThreadPoolExecutor):
        def submit(self, function, /, *arguments, **keywords):  # type: ignore[no-untyped-def]
            def record(*inner: object, **also: object) -> object:
                threads.add(threading.current_thread().name)

                return function(*inner, **also)

            return super().submit(record, *arguments, **keywords)

    with Naming(1, thread_name_prefix="pool-under-test") as pool:
        set_executor(pool)

        config = DynamicConfig(Database, key="db").file("config.toml")
        await config.init_async()

    assert any(name.startswith("pool-under-test") for name in threads)


def test_configure_executor_builds_a_named_pool() -> None:
    pool = configure_executor(2)

    assert default_executor() is pool

    names: list[str] = []
    list(pool.map(lambda _: names.append(threading.current_thread().name), range(4)))

    assert all(name.startswith("dynamic-config-blocking") for name in names), names


def test_configure_executor_refuses_a_pool_of_nothing() -> None:
    with pytest.raises(ValueError, match="cannot run anything"):
        configure_executor(0)


def test_configuring_twice_replaces_and_shuts_the_first_down() -> None:
    first = configure_executor(1)
    second = configure_executor(1)

    assert default_executor() is second

    with pytest.raises(RuntimeError):
        first.submit(lambda: None)


def test_a_pool_the_caller_passed_is_never_shut_down() -> None:
    """Theirs to close: the rest of their program may still be using it."""
    mine = ThreadPoolExecutor(1)

    set_executor(mine)
    set_executor(None)

    assert mine.submit(lambda: 42).result(5) == 42

    mine.shutdown()


def test_the_block_restores_what_it_found() -> None:
    outer = configure_executor(1)

    with executor(workers=1) as inner:
        assert default_executor() is inner
        assert inner is not outer

    assert default_executor() is outer

    with pytest.raises(RuntimeError):
        # The block built that one, so the block closed it.
        inner.submit(lambda: None)  # type: ignore[union-attr]


def test_the_block_leaves_a_borrowed_pool_alone() -> None:
    borrowed = ThreadPoolExecutor(1)

    with executor(borrowed):
        assert default_executor() is borrowed

    assert borrowed.submit(lambda: 42).result(5) == 42

    borrowed.shutdown()


def test_the_module_exports_what_the_documentation_names() -> None:
    for name in ("configure_executor", "executor", "set_executor"):
        assert name in dynamic_config.__all__
