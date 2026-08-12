"""An asyncio service that follows its configuration.

python examples/04_asyncio_service.py
"""

from __future__ import annotations

import asyncio

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig


class Pool:
    """Stands in for a connection pool that has to be resized."""

    def __init__(self, size: int) -> None:
        self.size = size

    async def resize(self, size: int) -> None:
        await asyncio.sleep(0)  # a real one would do real work here
        print(f"  pool resized {self.size} → {size}")
        self.size = size


async def follow(config: DynamicConfig[Database], pool: Pool) -> None:
    """One task, following every reload for the life of the service."""
    async for db in config.changes():
        if db.pool_size != pool.size:
            await pool.resize(db.pool_size)


async def serve(config: DynamicConfig[Database]) -> None:
    for request in range(1, 4):
        # Once per request, reused for the whole request: a reload landing
        # halfway through must not show one request two configurations.
        db = config.current()
        await asyncio.sleep(0.05)
        print(f"  request {request} used {db.host}, pool {db.pool_size}")


async def main() -> None:
    """Runs the asyncio service example end to end."""
    with workspace() as path:
        config = DynamicConfig(Database, key="db").file(str(path))
        await config.init_async()

        pool = Pool(config.current().pool_size)
        follower = asyncio.create_task(follow(config, pool))

        show("serving")
        await serve(config)

        show("a deployment edits the file")
        path.write_text('[db]\nhost = "db.internal"\npool_size = 32\n')
        await config.reload_async()
        await asyncio.sleep(0.1)

        await serve(config)

        follower.cancel()


if __name__ == "__main__":
    asyncio.run(main())
