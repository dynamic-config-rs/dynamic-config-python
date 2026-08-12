"""The decorator, for people who like their settings on the class.

python examples/05_decorator.py
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from _shared import show, workspace
from dynamic_config import dynamic_config


def main() -> None:
    """Runs the decorator example end to end."""
    with workspace() as path:

        @dynamic_config(key="db", files=[str(path)], env="APP_")
        class Database(BaseModel):
            host: str = "localhost"
            port: int = 5432
            pool_size: int = Field(default=8, ge=1)

        show("nothing has loaded yet")
        print(f"  try_current() → {Database.try_current()}")
        print("  import time is not load time; init() is a deliberate act")

        Database.config.init()

        show("after init")
        print(f"  current()   → {Database.current().host}")
        print(f"  source_of() → {Database.source_of('pool_size')}")

        # Everything else is on `Model.config`, which is the same object
        # the class API hands you.
        print(f"  the config  → {Database.config!r}")


if __name__ == "__main__":
    main()
