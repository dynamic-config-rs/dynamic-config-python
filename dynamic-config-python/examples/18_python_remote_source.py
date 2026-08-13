"""A remote store written in Python, on the fetch path.

The seven store crates (etcd, Consul, Vault, NATS, Redis, S3, Firestore)
stay in Rust and out of this wheel. What ships instead is the door: any
object with `fetch()` and `describe()` is a remote store here — a
company's own service, a file a sidecar writes, an API nobody will ever
write a Rust client for.

python examples/18_python_remote_source.py
"""

from __future__ import annotations

import json
import threading
import time

from _shared import Database, show, workspace
from dynamic_config import (
    AuthError,
    DynamicConfig,
    DynamicConfigError,
    Format,
    RemoteSource,
)


class ConfigService(RemoteSource):
    """A store that would be an HTTP call, standing in for one here."""

    def __init__(self) -> None:
        self.pool_size = 32
        self.credential_expired = False

    def fetch(self) -> tuple[str, Format]:
        # In a real one: `httpx.get(URL, timeout=5).raise_for_status()`.
        # The timeout belongs *here*, in the client, because nothing on the
        # Rust side can interrupt Python that has decided not to return.
        time.sleep(0.2)

        if self.credential_expired:
            # `AuthError` rather than any other exception, because the
            # distinction survives the crossing: a caller can back off on a
            # store that is merely unreachable and stop on one that refused
            # its credential.
            raise AuthError("the service rejected the token")

        return json.dumps({"db": {"pool_size": self.pool_size}}), Format.JSON

    def describe(self) -> str:
        # Asked once, when the source is installed. It is what provenance
        # and every remote error report, so name the store — never the
        # credential that reaches it.
        return "the config service"


def main() -> None:
    """Runs the Python remote source example end to end."""
    with workspace() as path:
        service = ConfigService()

        config = DynamicConfig(Database, key="db").file(str(path)).remote(service)

        show("fetching is explicit")
        config.init()
        print(f"  before any fetch: pool_size {config.current().pool_size}  ← the file")

        # `refresh_remote()` reads the store and keeps the document; a load
        # merges what was kept. Configuration is read on nearly every
        # request, and a round trip there would be indefensible.
        config.refresh_remote()
        config.reload()
        print(
            f"  after refresh:    pool_size {config.current().pool_size}  ← the store"
        )

        show("where the value came from")
        origin = config.source_of("pool_size")
        print(f"  {origin}")

        show("the GIL is not held across the fetch")
        # The fetch above sleeps 200 ms the way a network call does. A
        # second thread keeps running throughout — a Python `fetch()` doing
        # I/O releases the GIL exactly as any other Python thread does,
        # which is why there is no worker thread behind this API.
        ticks = 0
        stop = threading.Event()

        def tick() -> None:
            nonlocal ticks
            while not stop.is_set():
                ticks += 1

        ticker = threading.Thread(target=tick)
        ticker.start()
        service.pool_size = 64
        config.refresh_remote()
        stop.set()
        ticker.join()

        config.reload()
        print(f"  a second thread ran {ticks:,} times during a 200 ms fetch")
        print(f"  and the refresh still landed: pool_size {config.current().pool_size}")

        show("a store having a bad afternoon changes nothing")
        service.credential_expired = True

        try:
            config.refresh_remote()
        except DynamicConfigError as failure:
            # `AuthError`, not `RemoteError`: the two are siblings, and the
            # difference is whether waiting could help.
            print(f"  {type(failure).__name__}: kind={failure.kind}")
            print(f"  cause: {type(failure.__cause__).__name__}")
            print("  the message is not repeated — a store's exception carries URLs")

        print(f"  still serving: pool_size {config.current().pool_size}")


if __name__ == "__main__":
    main()
