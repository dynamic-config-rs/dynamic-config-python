"""The whole idea in twenty lines.

python examples/01_quick_start.py
"""

from __future__ import annotations

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig


def main() -> None:
    """Runs the quick start example end to end."""
    with workspace() as path:
        # The attribute declares nothing here — Python has no attribute —
        # so the model *is* the declaration and the builder says where the
        # values live.
        config = DynamicConfig(Database, key="db").file(str(path)).env("APP_")
        config.init()

        show("what loaded")
        db = config.current()
        print(f"  {db.host}:{db.port}, pool of {db.pool_size}")

        show("where each value came from")
        for field in ("host", "port", "pool_size"):
            origin = config.source_of(field)
            print(f"  {field:<10} {origin if origin else 'the model default'}")


if __name__ == "__main__":
    main()
