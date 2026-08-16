"""A remote store whose client is async.

python examples/25_async_remote_source.py
"""

from __future__ import annotations

import asyncio
import json

from _shared import Database, show, workspace
from dynamic_config import AsyncRemoteSource, DynamicConfig, Format


class ControlPlane(AsyncRemoteSource):
    """What an `httpx.AsyncClient` call would look like.

    A real one would be::

        async with httpx.AsyncClient() as client:
            response = await client.get(URL, timeout=5)
            response.raise_for_status()

            return response.text, Format.JSON
    """

    def __init__(self) -> None:
        self.pool_size = 24

    async def fetch(self) -> tuple[str, Format]:
        await asyncio.sleep(0.05)  # the round trip

        document = json.dumps({"db": {"pool_size": self.pool_size}})

        return document, Format.JSON

    def describe(self) -> str:
        # Named for provenance and for error messages — the store, never
        # the credential that reaches it.
        return "the control plane"


async def main() -> None:
    """Runs the async remote-source example end to end."""
    with workspace() as path:
        store = ControlPlane()
        config = DynamicConfig(Database, key="db").file(str(path)).remote(store)

        show("the file alone")
        await config.init_async()
        print(f"  pool_size {config.current().pool_size}, from the file")

        show("fetch, then load")
        # Awaited on this loop — which is where the client's own loop is,
        # and the only place it can run.
        await config.refresh_remote_async()
        await config.reload_async()

        print(f"  pool_size {config.current().pool_size}, from {store.describe()}")
        print(f"  provenance {config.source_of('pool_size')}")

        show("the store changes its mind")
        store.pool_size = 64
        await config.refresh_remote_async()
        await config.reload_async()

        print(f"  pool_size {config.current().pool_size}")

        show("what the synchronous call says")

        try:
            config.refresh_remote()
        except RuntimeError as refusal:
            print(f"  {refusal}")


if __name__ == "__main__":
    asyncio.run(main())
