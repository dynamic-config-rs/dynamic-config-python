"""Several configuration files, one service, one event loop.

    python examples/13_asyncio_many_files.py

A real service rarely has *one* configuration. It has a database section
owned by one team, a cache section owned by another, and a pile of
feature flags that change hourly — three files with three lifetimes, and
no reason for a change to one to disturb the other two.

`DynamicConfig` is a value, so this is just three of them: loaded
concurrently with `asyncio.gather`, watched independently, and followed
by one task each. The blocking half of every load runs on an executor of
this service's own, so a configuration reload never queues behind
whatever else the default pool is doing.
"""

from __future__ import annotations

import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, Field

import dynamic_config
from _shared import show
from dynamic_config import DynamicConfig


class Database(BaseModel):
    host: str
    pool_size: int = Field(default=8, ge=1)


class Cache(BaseModel):
    url: str
    ttl_seconds: int = Field(default=60, ge=1)


class Features(BaseModel):
    new_checkout: bool = False
    beta_search: bool = False


FILES = {
    "database": ("database.toml", '[database]\nhost = "db.internal"\npool_size = 8\n'),
    "cache": (
        "cache.toml",
        '[cache]\nurl = "redis://cache.internal"\nttl_seconds = 60\n',
    ),
    "features": ("features.toml", "[features]\nnew_checkout = false\n"),
}


async def follow(name: str, config: DynamicConfig[BaseModel]) -> None:
    """One task per configuration, for the life of the service."""
    async for value in config.changes():
        print(f"  [{name}] reloaded → {value!r}")


async def main() -> None:
    """Runs the asyncio many files example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        for _, (filename, document) in FILES.items():
            (root / filename).write_text(document)

        # Configuration gets its own two threads, so a reload never waits
        # behind an unrelated batch job on the default executor. This is
        # the Python-side twin of the Rust crate's `set_blocking_executor`.
        pool = ThreadPoolExecutor(2, thread_name_prefix="config")
        dynamic_config.set_executor(pool)

        database: DynamicConfig[Database] = DynamicConfig(
            Database, key="database"
        ).file(str(root / "database.toml"))
        cache: DynamicConfig[Cache] = DynamicConfig(Cache, key="cache").file(
            str(root / "cache.toml")
        )
        features: DynamicConfig[Features] = DynamicConfig(
            Features, key="features"
        ).file(str(root / "features.toml"))

        show("loading three files at once")
        # Three files, three parses, one await: they do not queue behind
        # each other the way three `init()` calls would.
        await asyncio.gather(
            database.init_async(), cache.init_async(), features.init_async()
        )

        db = database.current()
        print(f"  database  {db.host}, pool {db.pool_size}")
        print(f"  cache     {cache.current().url}, ttl {cache.current().ttl_seconds}")
        print(f"  features  new_checkout={features.current().new_checkout}")

        show("each one watched, each one followed")
        followers = [
            asyncio.create_task(follow(name, config))  # type: ignore[arg-type]
            for name, config in (
                ("database", database),
                ("cache", cache),
                ("features", features),
            )
        ]
        watches = [
            config.watch(debounce=0.05, poll_interval=0.05)
            for config in (database, cache, features)
        ]

        # A flag flips. The other two configurations do not notice, do not
        # re-parse, and do not wake their followers.
        deadline = asyncio.get_running_loop().time() + 15
        while not features.current().new_checkout:
            if asyncio.get_running_loop().time() > deadline:
                raise SystemExit("the watcher never landed")

            (root / "features.toml").write_text("[features]\nnew_checkout = true\n")
            await asyncio.sleep(0.1)

        await asyncio.sleep(0.2)

        show("state afterwards")
        print(f"  database generation {database.generation}  (untouched)")
        print(f"  cache generation    {cache.generation}  (untouched)")
        print(f"  features generation {features.generation}  (moved)")

        for task in followers:
            task.cancel()
        for watch in watches:
            watch.stop()

        dynamic_config.set_executor(None)
        pool.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
