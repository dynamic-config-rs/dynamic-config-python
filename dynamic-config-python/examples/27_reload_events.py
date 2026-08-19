"""The diagnostic stream: what installed, what was refused.

python examples/27_reload_events.py
"""

from __future__ import annotations

import asyncio
import contextlib

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig, DynamicConfigError, Reloaded, ReloadFailed


async def watch_events(config: DynamicConfig[Database]) -> None:
    """What a log line, a metric or an alert is built from."""
    # A refusal wakes this stream natively — the engine's failure hook
    # signals the same parked thread an install does, so nothing polls.
    async for event in config.events():
        if isinstance(event, ReloadFailed):
            print(
                f"  ✗ refused at generation {event.generation}: {event.kind}"
                f" ({event.consecutive} in a row)"
            )
        elif isinstance(event, Reloaded):
            moved = ", ".join(event.changed) or "the first install"
            print(f"  ✓ generation {event.generation} — {moved} [{event.reason}]")


async def main() -> None:
    """Runs the events example end to end."""
    with workspace() as path:
        config = DynamicConfig(Database, key="db").file(str(path))
        await config.init_async()

        watching = asyncio.create_task(watch_events(config))
        await asyncio.sleep(0.05)

        show("two good deployments")

        for size in (24, 32):
            path.write_text(f'[db]\nhost = "db.internal"\npool_size = {size}\n')
            await config.reload_async()
            await asyncio.sleep(0.15)

        show("one bad one")
        path.write_text(
            '[db]\nhost = "db.internal"\npool_size = "as many as it takes"\n'
        )

        # The stream reports it; the caller already knows.
        with contextlib.suppress(DynamicConfigError):
            await config.reload_async()

        await asyncio.sleep(0.25)

        show("and the fix")
        path.write_text('[db]\nhost = "db.internal"\npool_size = 40\n')
        await config.reload_async()
        await asyncio.sleep(0.15)

        watching.cancel()

        show("what is not in any of those events")
        print("  a value. Paths, kinds, counts and timestamps only — a")
        print("  value in an event is a secret in a log.")


if __name__ == "__main__":
    asyncio.run(main())
