"""Async reload hooks, and what to do when installs outrun them.

python examples/24_async_callbacks.py
"""

from __future__ import annotations

import asyncio

from _shared import Database, show, workspace
from dynamic_config import Backpressure, Dispatch, DynamicConfig


class Pool:
    """Stands in for a pool whose resize takes real time."""

    def __init__(self, size: int) -> None:
        self.size = size

    async def resize(self, size: int) -> None:
        await asyncio.sleep(0.15)  # a real one talks to a database here
        print(f"  pool resized {self.size} → {size}")
        self.size = size


async def main() -> None:
    """Runs the async-callback example end to end."""
    with workspace() as path:
        config = DynamicConfig(Database, key="db").file(str(path))
        await config.init_async()

        pool = Pool(config.current().pool_size)

        # `on_reload_async` is `on_reload` with `dispatch=Dispatch.ASYNCIO`:
        # the watcher schedules the task and returns, so a slow callback
        # delays nothing but itself.
        @config.on_reload_async
        async def resize(_previous: Database | None, current: Database) -> None:
            await pool.resize(current.pool_size)

        show("three deployments, faster than the pool can follow")

        for size in (24, 32, 48):
            path.write_text(f'[db]\nhost = "db.internal"\npool_size = {size}\n')
            await config.reload_async()
            print(f"  reload returned with pool_size {size} installed")
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.6)

        show("what the default policy did")
        print("  latest wins: the first call ran, the last was kept, the")
        print("  middle one was dropped — resizing to a size nobody wants")
        print("  any more is work done for nothing.")

        show("the same hook, keeping every install instead")

        second = DynamicConfig(Database, key="db").file(str(path))
        await second.init_async()
        seen: list[int] = []

        async def record(_previous: Database | None, current: Database) -> None:
            await asyncio.sleep(0.05)
            seen.append(current.pool_size)

        second.on_reload_async(record, backpressure=Backpressure.SERIAL)

        for size in (64, 80, 96):
            path.write_text(f'[db]\nhost = "db.internal"\npool_size = {size}\n')
            await second.reload_async()
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.6)
        print(f"  serial dropped nothing: {seen}")

        show("and a synchronous hook that is simply slow")

        third = DynamicConfig(Database, key="db").file(str(path))
        await third.init_async()
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def rewrite_a_template(_previous: Database | None, _current: Database) -> None:
            import time

            time.sleep(0.1)  # blocking, and off the installing thread
            loop.call_soon_threadsafe(done.set)

        third.on_reload(rewrite_a_template, dispatch=Dispatch.EXECUTOR)

        path.write_text('[db]\nhost = "db.internal"\npool_size = 100\n')
        started = asyncio.get_running_loop().time()
        await third.reload_async()
        print(
            f"  the reload returned in "
            f"{(asyncio.get_running_loop().time() - started) * 1000:.0f} ms, "
            "with the hook still running"
        )

        await asyncio.wait_for(done.wait(), 5)
        print("  the hook finished afterwards, on the configuration executor")


if __name__ == "__main__":
    asyncio.run(main())
