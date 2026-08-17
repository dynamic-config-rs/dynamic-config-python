"""Reacting to a reload: every shape a callback takes here.

    python examples/16_callbacks.py

Loading configuration is the easy half. The half that decides whether hot
reload is *useful* is what happens next: a pool that has to be resized, a
client that has to be rebuilt, an audit line somebody will read at three
in the morning.

Six shapes, in the order you tend to need them:

    config.on_reload(hook)              every install
    @config.on_reload                   the same, as a decorator
    @config.on_change("pool.max_size")  only when that path moved
    with config.on_reload(hook):        registered for a scope
    config.on_reload(hook, dispatch=…)  somewhere other than this thread
    async for model in config.changes() no callback at all

The rule underneath all of them: **compare, then signal the thing that
owns the resource.** By default a hook runs on whichever thread reloaded,
so it is not the place to rebuild a connection pool — it is the place to
tell the pool to rebuild itself, or to say `dispatch=` and have the
rebuild happen somewhere the reload is not waiting.

[`24_async_callbacks.py`](24_async_callbacks.py) is the async half in
full: every backpressure policy, and what each one drops.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

from _shared import show
from dynamic_config import Dispatch, DynamicConfig, changed_paths

# To stdout, so the hook output interleaves with the narration in the
# order it actually happened rather than arriving in a block at the end.
logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=sys.stdout)
log = logging.getLogger("example")


class Pool(BaseModel):
    max_size: int = Field(default=8, ge=1, le=1000)
    timeout_seconds: float = 5.0


class Service(BaseModel):
    host: str = "localhost"
    port: int = 8080
    pool: Pool = Pool()


class ConnectionPool:
    """Stands in for the thing a real service has to keep in step."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.rebuilds = 0

    def resize(self, size: int) -> None:
        """Cheap: a number changes, nothing reconnects."""
        self.size = size

    def rebuild(self) -> None:
        """Expensive: this is what you do not want on every reload."""
        self.rebuilds += 1


def write(
    path: Path,
    *,
    host: str = "db.internal",
    port: int = 8080,
    max_size: int = 8,
    timeout: float = 5.0,
) -> None:
    """Writes the whole section, so every field is always present."""
    path.write_text(
        f'[svc]\nhost = "{host}"\nport = {port}\n\n'
        f"[svc.pool]\nmax_size = {max_size}\ntimeout_seconds = {timeout}\n"
    )


def main() -> None:
    """Runs the callbacks example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "service.toml"
        write(path)

        pool = ConnectionPool(size=8)
        config = DynamicConfig(Service, key="svc").file(str(path))

        # ── 1. The audit hook: what moved, never what to ────────────────
        #
        # Registered *before* the first load, so it sees that one too:
        # `old` is None exactly once, which is how a hook tells "starting
        # up" from "somebody changed something".
        @config.on_reload
        def audit(old: Service | None, new: Service) -> None:
            if old is None:
                log.info("loaded: %s:%s", new.host, new.port)
                return

            moved = ", ".join(str(change) for change in changed_paths(old, new))
            log.info("reloaded: %s", moved)

        config.init()

        show("1. the decorator keeps the function")
        # The guard forwards calls, so `audit` is still the function it
        # looks like — and still the registration, which is what you need
        # to unregister it.
        print(f"  audit is callable: {callable(audit)}")
        print(f"  audit.hook.__name__: {audit.hook.__name__}")

        # ── 2. The filter: react to one path, not to every install ──────
        show("2. on_change fires only when its path moved")

        @config.on_change("pool.max_size")
        def resize(_old: Service | None, new: Service) -> None:
            pool.resize(new.pool.max_size)

        @config.on_change("host", "port")
        def reconnect(_old: Service | None, _new: Service) -> None:
            # The expensive one. A neighbouring field moving must not
            # trigger it, which is the whole point of the filter.
            pool.rebuild()

        write(path, port=9090)  # the address moved
        config.reload()
        print(f"  after an address change: rebuilds={pool.rebuilds} size={pool.size}")

        write(path, port=9090, max_size=32)  # only the pool size moved
        config.reload()
        print(f"  after a pool change:     rebuilds={pool.rebuilds} size={pool.size}")

        write(path, port=9090, max_size=32, timeout=2.5)  # neither moved
        config.reload()
        print(f"  after an unrelated edit: rebuilds={pool.rebuilds} size={pool.size}")

        # ── 3. Scoped registration ──────────────────────────────────────
        show("3. a hook that only exists for a block")
        during: list[str] = []

        with config.on_reload(lambda _old, new: during.append(new.host)):
            write(path, host="db.replica", port=9090, max_size=32)
            config.reload()

        write(path, host="db.third", port=9090, max_size=32)
        config.reload()

        print(f"  saw {during} — the second reload was outside the block")

        # ── 4. Handing work off a hook ──────────────────────────────────
        #
        # A hook runs on the thread that reloaded, and that thread is the
        # watcher's. Anything slow, anything that needs a lock, anything
        # that belongs to another thread: hand it over and return.
        show("4. hand the work to whoever owns it")
        work: queue.Queue[Service] = queue.Queue()
        done = threading.Event()

        def worker() -> None:
            """The subsystem that owns the resource, doing its own work."""
            model = work.get()
            log.info("worker rebuilt for %s:%s", model.host, model.port)
            done.set()

        thread = threading.Thread(target=worker, name="pool-owner")
        thread.start()

        with config.on_reload(lambda _old, new: work.put(new)):
            write(path, host="db.fourth", port=9090, max_size=32)
            config.reload()

        done.wait(timeout=5)
        thread.join(timeout=5)

        # ── 5. Or say where it should run, and let go of the queue ──────
        #
        # The hand-off above is what `dispatch=` does for you when the
        # destination is "not the installing thread". The queue is still
        # the answer when the destination is a thread of your own that is
        # already running; this is the answer when it is not.
        show("5. dispatch: the same hand-off, as a parameter")
        finished = threading.Event()

        def rebuild_slowly(_old: Service | None, new: Service) -> None:
            """Blocking work, and not on the thread that installed."""
            time.sleep(0.1)
            log.info(
                "rebuilt on %s for %s",
                threading.current_thread().name,
                new.host,
            )
            finished.set()

        with config.on_reload(rebuild_slowly, dispatch=Dispatch.EXECUTOR):
            write(path, host="db.fifth", port=9090, max_size=32)
            started = time.perf_counter()
            config.reload()
            elapsed = (time.perf_counter() - started) * 1000

            print(f"  reload returned in {elapsed:.0f} ms, hook still running")
            finished.wait(timeout=5)

        # ── 6. No callback at all ───────────────────────────────────────
        show("6. on an event loop, a callback is optional")
        asyncio.run(follow(config, path))

        show("what the hooks left behind")
        print(f"  pool size     {pool.size}   (resize, on pool.max_size)")
        print(f"  pool rebuilds {pool.rebuilds}   (reconnect, on host/port)")
        print(f"  generation    {config.generation}")
        print(
            "  more rebuilds than section 2 showed: sections 3 to 6 each moved\n"
            "  the host, and a filter that fires when its path moves is a filter\n"
            "  doing its job"
        )

        audit.close()
        resize.close()
        reconnect.close()


async def follow(config: DynamicConfig[Service], path: Path) -> None:
    """Consumes installs as an async iterator instead of a callback.

    Same events, read rather than pushed — which is usually what you want
    on a loop, because the body runs *on the loop* and can await.
    """
    seen: list[int] = []

    async def consume() -> None:
        async for model in config.changes():
            seen.append(model.port)
            await asyncio.sleep(0)  # stand-in for real async work
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)

    write(path, host="db.fourth", port=7070, max_size=32)
    await config.reload_async()
    await asyncio.wait_for(task, timeout=5)

    print(f"  the async follower saw port {seen[0]} without a callback")


if __name__ == "__main__":
    main()
