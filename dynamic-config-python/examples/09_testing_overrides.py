"""Pinning configuration in a test, without touching the filesystem.

    python examples/09_testing_overrides.py

The override layer outranks every source, which is what makes a test
authoritative about what the code under test sees.
"""

from __future__ import annotations

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig


def under_test(config: DynamicConfig[Database]) -> str:
    """The code a test wants to steer."""
    db = config.current()

    return f"connecting to {db.host}:{db.port} with {db.pool_size} connections"


def main() -> None:
    """Runs the testing overrides example end to end."""
    with workspace() as path:
        config = DynamicConfig(Database, key="db").file(str(path))
        config.init()

        show("as configured")
        print(f"  {under_test(config)}")

        show("pinned for a test")
        config.set_override("host", "localhost")
        config.set_override("pool_size", 1)
        config.reload()
        print(f"  {under_test(config)}")

        show("and back")
        config.clear_overrides()
        config.reload()
        print(f"  {under_test(config)}")

        show("or built by hand, with no sources at all")
        standalone = DynamicConfig(Database, key="db")
        standalone.replace(Database(host="in-memory", port=1, pool_size=2))
        print(f"  {under_test(standalone)}")


if __name__ == "__main__":
    main()
