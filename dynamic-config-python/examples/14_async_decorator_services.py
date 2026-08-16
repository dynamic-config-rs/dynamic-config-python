"""Several decorated models, one event loop, no object passed around.

    python examples/14_async_decorator_services.py

Example 13 does this with `DynamicConfig` values, which is the right
shape when something owns them. This is the other shape: the
configuration lives *on the model class*, so any module that can import
`Database` can ask `Database.current()` without being handed a
configuration object first — the thing a global gives you, without the
thing a global costs you.

Three decorated classes, three files, one loop. Each is loaded
concurrently, watched independently and followed by its own task, and
the async surface reaches all of it through `Model.config`:

    await Database.config.init_async()      # load without blocking the loop
    await Database.config.reload_async()    # and again, on demand
    async for model in Database.config.changes():
        ...                                 # every install from here on

`Model.current()` stays synchronous everywhere, because it is an
attribute lookup on a cached instance — there is nothing to await.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

from pydantic import BaseModel, Field

from _shared import show
from dynamic_config import dynamic_config


def write(directory: Path) -> dict[str, Path]:
    """Writes the three files this service reads, and returns them by name."""
    files = {
        "db": directory / "database.toml",
        "cache": directory / "cache.toml",
        "flags": directory / "flags.toml",
    }

    files["db"].write_text('[db]\nhost = "db.internal"\npool_size = 8\n')
    files["cache"].write_text('[cache]\nurl = "redis://localhost"\nttl_seconds = 60\n')
    files["flags"].write_text("[flags]\nnew_checkout = false\ndark_mode = true\n")

    return files


async def main() -> None:
    """Runs the async decorator example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        files = write(Path(directory))

        # Decorated at definition, loaded later: importing a module should
        # not begin filesystem work, so `init=False` (the default) is the
        # whole reason this is safe to do at module level in a real app.
        @dynamic_config(key="db", files=[str(files["db"])], env="APP_")
        class Database(BaseModel):
            host: str = "localhost"
            pool_size: int = Field(default=8, ge=1, le=1000)

        @dynamic_config(key="cache", files=[str(files["cache"])], env="APP_")
        class Cache(BaseModel):
            url: str = "redis://localhost"
            ttl_seconds: int = Field(default=60, ge=1)

        @dynamic_config(key="flags", files=[str(files["flags"])], env="APP_")
        class Flags(BaseModel):
            new_checkout: bool = False
            dark_mode: bool = False

        services = (Database, Cache, Flags)

        show("three configurations, loaded concurrently")
        # Three loads, three threads, one await: the loop is free while the
        # files are read and parsed.
        await asyncio.gather(*(service.config.init_async() for service in services))

        print(f"  Database.current() → {Database.current().host}")
        print(f"  Cache.current()    → {Cache.current().url}")
        print(f"  Flags.current()    → new_checkout={Flags.current().new_checkout}")
        print("  and any module that imports the class can ask the same thing")

        show("a follower per configuration")
        seen: dict[str, list[str]] = {"db": [], "cache": [], "flags": []}

        async def follow(name: str, service: type) -> None:
            """Appends a line per install, until cancelled."""
            async for model in service.config.changes():
                seen[name].append(repr(model))

        followers = [
            asyncio.create_task(follow(name, service))
            for name, service in zip(seen, services)
        ]

        # `AsyncExitStack` rather than a list of handles and a `finally`
        # that stops each: a watcher started in a block cannot be left
        # running by an exception on the way out of it.
        async with AsyncExitStack() as watching:
            for service in services:
                await watching.enter_async_context(
                    service.config.watching_async(debounce=0.05, poll_interval=0.05)
                )

            # One file changes. The other two configurations do not move —
            # separate engines, separate generations, separate followers.
            show("one team edits one file")
            deadline = asyncio.get_running_loop().time() + 20

            while not seen["flags"] and asyncio.get_running_loop().time() < deadline:
                files["flags"].write_text(
                    "[flags]\nnew_checkout = true\ndark_mode = true\n"
                )
                await asyncio.sleep(0.1)

            print(f"  Flags.current()  → new_checkout={Flags.current().new_checkout}")
            print(f"  flags generation → {Flags.config.generation}")
            print(f"  db generation    → {Database.config.generation} (untouched)")
            print(f"  cache generation → {Cache.config.generation} (untouched)")

            show("and an explicit reload is the same call, awaited")
            files["db"].write_text('[db]\nhost = "db.replica"\npool_size = 32\n')
            await Database.config.reload_async()
            print(f"  Database.current() → {Database.current().host}")
            print(f"  source_of('host')  → {Database.source_of('host')}")

        # One turn of the loop before cancelling: the notifier resolves a
        # waiting task immediately, but "immediately" is still after the
        # reload's caller has been given control back.
        await asyncio.sleep(0.05)

        for follower in followers:
            follower.cancel()

        show("what each follower saw")
        for name, lines in seen.items():
            print(f"  {name}: {len(lines)} install(s) after the first")


if __name__ == "__main__":
    asyncio.run(main())
