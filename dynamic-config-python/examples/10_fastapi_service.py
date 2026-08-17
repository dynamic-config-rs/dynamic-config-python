"""FastAPI: configuration as a dependency, for both kinds of endpoint.

    pip install fastapi httpx
    python examples/10_fastapi_service.py

FastAPI runs `async def` endpoints on the event loop and plain `def`
endpoints on a worker thread. Configuration is read the same way in both:
`config.current()` is an attribute lookup on a cached model, so it needs
no `await` on the loop and blocks nothing on a thread.

The rule that matters is the second line of every handler: **read once,
then use that value for the whole request.** A reload landing halfway
through would otherwise show one request two configurations.

Loading and watching belong to the app's lifespan, and
`config.running_async()` is exactly that block: load, watch, serve, stop.
The loop that starts a service is the loop that will answer its requests,
and neither the load nor the registration is something it should wait on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig

try:
    from fastapi import Depends, FastAPI
except ImportError:  # pragma: no cover - the example says how to fix it
    raise SystemExit("this example needs FastAPI: pip install fastapi httpx") from None


def build(path: Path) -> tuple[FastAPI, DynamicConfig[Database], object]:
    config = DynamicConfig(Database, key="db").file(str(path)).env("APP_")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Loads, watches, serves, stops — the app's whole lifetime.

        `running_async()` is the three of those as one block, which is
        what a lifespan already is: startup and shutdown in one function,
        so the watcher cannot be started without also being stopped and
        there is no second handler to keep in step.

        It loads with `init_async` and starts the watcher with
        `watch_async`, both for the same reason: the loop that starts a
        service is the loop that will answer its requests, and neither
        call should be the thing it waits on. Starting a watcher is short
        but not free — it resolves the directories to observe, registers
        each with the platform's notification backend and spawns the
        thread that carries events. Native registration is a fraction of
        a millisecond; polling first scans everything it watches, which
        is milliseconds over a large directory and worse over a network
        filesystem.

        Stopping needs no await, even here: it drops the backend and
        returns without joining the watcher thread or waiting out a
        debounce window, so there is nothing to hand off.

        Here rather than at import: importing a module should not begin
        filesystem work. And one watcher per app — a second `watch()` on
        one configuration is `AlreadyExists`, deliberately, because two
        watchers on one file could only mislead.
        """
        async with config.running_async(debounce=0.25):
            yield

    application = FastAPI(lifespan=lifespan)

    def current_config() -> Database:
        """The dependency: one attribute lookup, no I/O, no validation.

        A plain `def` dependency, deliberately — FastAPI would run an
        `async def` one on the loop and a `def` one on a thread, and this
        is cheap enough that neither choice matters. Being sync means a
        sync endpoint does not pay for a loop round trip.
        """
        return config.current()

    @application.get("/async/health")
    async def health_async(db: Database = Depends(current_config)) -> dict[str, object]:
        # On the event loop. `current()` needs no await, so the handler
        # never yields just to read its own configuration.
        await asyncio.sleep(0)  # stand-in for real async work
        return {"style": "async", "host": db.host, "pool": db.pool_size}

    @application.get("/sync/health")
    def health_sync(db: Database = Depends(current_config)) -> dict[str, object]:
        # On a worker thread. The same read, and it is thread-safe: the
        # model is immutable and the swap is atomic, so a reload landing
        # mid-handler cannot tear this value.
        return {"style": "sync", "host": db.host, "pool": db.pool_size}

    @application.get("/async/explain")
    async def explain(path: str = "pool_size") -> dict[str, object]:
        # Diagnostics do read the sources, so they are real work: keep
        # them off the loop.
        rendered = await asyncio.to_thread(lambda: str(config.explain(path)))

        return {"path": path, "explanation": rendered}

    # The dependency is returned as well as used: overriding it in a test
    # means naming it, which is why a real app defines it at module level
    # and imports it from the test.
    return application, config, current_config


def main() -> None:
    """Runs the fastapi service example end to end."""
    from fastapi.testclient import TestClient

    with workspace() as path:
        application, config, current_config = build(path)

        with TestClient(application) as client:
            show("both endpoint styles, same configuration")
            print(f"  async → {client.get('/async/health').json()}")
            print(f"  sync  → {client.get('/sync/health').json()}")

            show("a deployment edits the file")
            path.write_text('[db]\nhost = "db.internal"\npool_size = 64\n')
            config.reload()

            print(f"  async → {client.get('/async/health').json()}")
            print(f"  sync  → {client.get('/sync/health').json()}")

            show("and the diagnostics are an endpoint too")
            body = client.get("/async/explain").json()
            for line in str(body["explanation"]).splitlines()[:4]:
                print(f"  {line}")

        show("overriding it in a test is FastAPI's own mechanism")
        application.dependency_overrides[current_config] = lambda: Database(
            host="localhost", pool_size=1
        )

        with TestClient(application) as client:
            print(f"  async → {client.get('/async/health').json()}")
            print(f"  sync  → {client.get('/sync/health').json()}")

        application.dependency_overrides.clear()


if __name__ == "__main__":
    main()
