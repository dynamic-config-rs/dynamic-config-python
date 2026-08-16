"""Several configurations under one lifecycle.

python examples/23_config_group.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel

from _shared import Database, show
from dynamic_config import ConfigGroup, DynamicConfig

DOCUMENT = """
[db]
host = "db.internal"
pool_size = 16

[cache]
url = "redis://cache.internal"
ttl = 60

[queue]
url = "amqp://queue.internal"
prefetch = 32
"""


class Cache(BaseModel):
    """A second configuration, in the same file and its own section."""

    url: str = "redis://localhost"
    ttl: int = 30


class Queue(BaseModel):
    """And a third."""

    url: str = "amqp://localhost"
    prefetch: int = 8


def main() -> None:
    """Runs the group example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(DOCUMENT)

        database = DynamicConfig(Database, key="db").file(str(path))
        cache = DynamicConfig(Cache, key="cache").file(str(path))
        queue = DynamicConfig(Queue, key="queue").file(str(path))

        # One object for the lifecycle — and nothing for the read path,
        # which stays `database.current()`.
        group = ConfigGroup(database, cache, queue)

        show("the whole lifetime as one block")

        with group.running(debounce=0.05):
            print(f"  database  {database.current().host}")
            print(f"  cache     {cache.current().url}")
            print(f"  queue     {queue.current().url}")
            print(f"  watching  {len(group)} configurations")

            show("one call for a health endpoint")

            for key, status in group.status().items():
                print(
                    f"  {key:9} generation {status.generation}, "
                    f"healthy {status.is_healthy}"
                )

            print(f"  generations {group.generations()}")

        show("the block ended, and every watcher with it")
        print("  nothing left running")


if __name__ == "__main__":
    main()
