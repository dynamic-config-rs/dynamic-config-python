"""Pinning configuration in a test, without touching the filesystem.

    python examples/09_testing_overrides.py

The override layer outranks every source, which is what makes a test
authoritative about what the code under test sees. `overrides(...)` is
that layer scoped to a block, which is what keeps one test's pin out of
the next test — and the fixtures the package ships as a pytest plugin
(`dynamic_config.pytest`) are what keep the *files* apart.
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

        show("pinned for a block, and put back after it")
        with config.overrides(host="localhost", pool_size=1):
            print(f"  {under_test(config)}")

            # A nested block restores the *previous* layer rather than
            # clearing it, so the outer pin survives this one ending.
            with config.overrides(pool_size=32):
                print(f"  {under_test(config)}")

            print(f"  {under_test(config)}")

        show("and back")
        print(f"  {under_test(config)}")

        show("still back, when the block ended the way a failing test does")
        try:
            with config.overrides(host="never-seen-again"):
                raise AssertionError("the assertion this test would have failed on")
        except AssertionError:
            pass

        print(f"  {under_test(config)}")

        show("the long hand, for a pin that outlives one block")
        config.set_override("host", "localhost")
        config.reload()
        print(f"  {under_test(config)}")
        config.clear_overrides()
        config.reload()

        show("or built by hand, with no sources at all")
        standalone = DynamicConfig(Database, key="db")
        standalone.replace(Database(host="in-memory", port=1, pool_size=2))
        print(f"  {under_test(standalone)}")


if __name__ == "__main__":
    main()
