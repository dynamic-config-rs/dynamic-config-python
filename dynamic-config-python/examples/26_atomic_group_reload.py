"""All of them install, or none of them does.

python examples/26_atomic_group_reload.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel

from _shared import show
from dynamic_config import ConfigGroup, DynamicConfig, DynamicConfigError


class Database(BaseModel):
    host: str = "localhost"
    pool_size: int = 8


class Cache(BaseModel):
    url: str = "redis://localhost"
    ttl: int = 30


def document(pool_size: int, ttl: object) -> str:
    return (
        f'[db]\nhost = "db.internal"\npool_size = {pool_size}\n'
        f'\n[cache]\nurl = "redis://cache.internal"\nttl = {ttl}\n'
    )


def report(database: DynamicConfig[Database], cache: DynamicConfig[Cache]) -> None:
    print(
        f"  db.pool_size {database.current().pool_size}, "
        f"generation {database.generation}"
    )
    print(f"  cache.ttl    {cache.current().ttl}, generation {cache.generation}")


def main() -> None:
    """Runs the atomic-reload example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(document(16, 60))

        database = DynamicConfig(Database, key="db").file(str(path))
        cache = DynamicConfig(Cache, key="cache").file(str(path))
        group = ConfigGroup(database, cache)
        group.init()

        show("as deployed")
        report(database, cache)

        show("a deployment that breaks one of the two")
        path.write_text(document(32, '"forever"'))

        try:
            group.reload_atomic()
        except DynamicConfigError as refusal:
            print(f"  refused: {refusal}")

        print("  and nothing installed — not even the half that parsed:")
        report(database, cache)

        show("for comparison, the per-member reload")

        try:
            group.reload()
        except DynamicConfigError as refusal:
            print(f"  refused: {str(refusal).splitlines()[0]}")

        print("  the database moved and the cache did not, which is the")
        print("  mixed state `reload_atomic` exists to prevent:")
        report(database, cache)

        show("the deployment is fixed")
        path.write_text(document(48, 90))
        group.reload_atomic()
        report(database, cache)


if __name__ == "__main__":
    main()
