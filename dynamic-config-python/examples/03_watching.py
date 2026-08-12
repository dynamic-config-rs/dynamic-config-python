"""Reloading on a file change, and reacting to it.

python examples/03_watching.py
"""

from __future__ import annotations

import time

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig, changed_paths


def main() -> None:
    """Runs the watching example end to end."""
    with workspace() as path:
        config = DynamicConfig(Database, key="db").file(str(path))
        config.init()

        # The audit half of a reload: which paths moved, never what to.
        config.on_reload(
            lambda old, new: print(
                "  reloaded: "
                + ", ".join(str(change) for change in changed_paths(old, new))
            )
        )

        show("watching")
        # `poll_interval` is what network and overlay filesystems need,
        # where the native backend registers and then never fires.
        with config.watch(debounce=0.05, poll_interval=0.05):
            print(f"  pool starts at {config.current().pool_size}")

            deadline = time.monotonic() + 15
            while config.current().pool_size != 64 and time.monotonic() < deadline:
                path.write_text('[db]\nhost = "db.internal"\npool_size = 64\n')
                time.sleep(0.1)

            print(f"  pool is now   {config.current().pool_size}")

            # A rejected edit changes nothing: `pool_size` has `ge=1`, so
            # Pydantic refuses and the previous model keeps serving.
            path.write_text('[db]\nhost = "db.internal"\npool_size = 0\n')
            time.sleep(0.5)
            print(f"  after a rejected edit, still {config.current().pool_size}")


if __name__ == "__main__":
    main()
