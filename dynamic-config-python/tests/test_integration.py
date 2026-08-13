"""End to end: whole scenarios, not single calls.

The rest of the suite tests one behaviour at a time. This file runs the
things a service actually does — start under a real web framework, watch
a file an operator edits, refuse a bad edit while still answering, come
back from a cache after a restart — and asserts what a user would
notice rather than what the call returned.

The framework tests import the shipped examples rather than rebuilding
their apps, so an example that rots fails here too.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, SecretStr

from dynamic_config import (
    DynamicConfig,
    Format,
    InvalidError,
    NotInitialisedError,
    Origin,
    RemoteSource,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

if str(EXAMPLES) not in sys.path:
    sys.path.insert(0, str(EXAMPLES))


class Service(BaseModel):
    """Correlated fields: a reader can tell a torn read from a whole one."""

    name: str = "unnamed"
    port: int = Field(default=1, ge=1)
    pool_size: int = Field(default=8, ge=1, le=1000)
    password: SecretStr = SecretStr("")


def write(port: int, *, name: str | None = None, path: str = "config.toml") -> None:
    """A file whose two fields always agree, so a reader can check them."""
    Path(path).write_text(
        f'[svc]\nname = "{name or f"service-{port}"}"\nport = {port}\n'
        'password = "hunter2"\n'
    )


# ── A service, from start to shutdown ──────────────────────────────────


async def test_a_service_starts_watches_reloads_and_stops(workspace: Path) -> None:
    """The whole arc, with every layer in play at once."""
    write(8000)
    Path(".env").write_text("DCTEST_SVC_POOL_SIZE=32\n")

    seen: list[tuple[int, int]] = []

    config = (
        DynamicConfig(Service, key="svc")
        .file("config.toml")
        .env_file(".env")
        .env("DCTEST_")
        .cache(".cache.json")
    )
    config.set_default("pool_size", 4)

    await config.init_async()

    guard = config.on_reload(lambda old, new: seen.append((new.port, new.pool_size)))
    watch = await config.watch_async(debounce=0.05, poll_interval=0.05)

    try:
        first = config.current()
        assert first.port == 8000
        assert first.pool_size == 32, "the .env layer beat the default"
        assert first.password.get_secret_value() == "hunter2"

        # An operator edits the file; nobody restarts anything.
        follower = asyncio.create_task(config.changed_async(timeout=20))
        deadline = asyncio.get_running_loop().time() + 20

        while not follower.done() and asyncio.get_running_loop().time() < deadline:
            write(9000)
            await asyncio.sleep(0.1)

        landed = await follower

        assert landed is not None, "the watcher never reported the edit"
        assert landed.port == 9000
        assert config.current().port == 9000
        assert config.generation >= 2
        assert seen, "the reload hook never ran"
        assert seen[-1][0] == 9000, "the reload hook saw the new model"
    finally:
        guard.close()
        watch.stop()

    # Shutdown: the hook is gone, the watcher is stopped, reads still work.
    before = len(seen)
    write(9100)
    await config.reload_async()

    assert config.current().port == 9100
    assert len(seen) == before, "a closed guard still ran its hook"
    assert not watch.running


async def test_a_bad_edit_never_reaches_a_running_service(workspace: Path) -> None:
    """The promise hot reload has to keep: a typo does not take you down."""
    write(8000)

    config = DynamicConfig(Service, key="svc").file("config.toml").cache(".cache.json")
    await config.init_async()

    watch = await config.watch_async(debounce=0.05, poll_interval=0.05)

    try:
        generation = config.generation

        # `pool_size = 0` violates the model; the file is valid TOML, so
        # this is rejected by validation rather than by parsing.
        Path("config.toml").write_text(
            '[svc]\nname = "broken"\nport = 1\npool_size = 0\n'
        )
        await asyncio.sleep(1.0)

        still = config.current()
        assert still.port == 8000, "a rejected reload replaced the served model"
        assert still.name == "service-8000"
        assert config.generation == generation, "a rejected reload bumped generation"

        # The explicit path reports it, rather than failing silently.
        with pytest.raises(InvalidError):
            await config.reload_async()

        assert config.current().port == 8000

        # And a good edit still lands afterwards: nothing latched.
        deadline = asyncio.get_running_loop().time() + 20

        while (
            config.current().port != 8100
            and asyncio.get_running_loop().time() < deadline
        ):
            write(8100)
            await asyncio.sleep(0.1)

        assert config.current().port == 8100, "the watcher stopped after a rejection"
    finally:
        watch.stop()


async def test_a_restart_recovers_from_the_last_known_good_cache(
    workspace: Path,
) -> None:
    """A restart into a broken deployment still comes up.

    Broken rather than missing: a file that is *absent* is skipped by
    design, and a model whose fields all have defaults loads cleanly
    without it, so there is nothing to recover from. What a restart
    actually meets is a half-written or hand-edited file, which fails to
    parse — and that is what the cache is for.
    """
    write(8000)

    first = DynamicConfig(Service, key="svc").file("config.toml").cache(".cache.json")
    await first.init_async()

    assert first.current().port == 8000
    assert Path(".cache.json").exists(), "a successful install wrote no cache"

    # A deploy that copied half a file.
    Path("config.toml").write_text('[svc]\nname = "trunc')

    second = DynamicConfig(Service, key="svc").file("config.toml").cache(".cache.json")
    await second.init_async()

    recovered = second.current()
    assert recovered.port == 8000, "the cache did not carry the last known good"
    assert recovered.name == "service-8000"
    assert recovered.password.get_secret_value() == "", (
        "the redacted cache carried a secret across the restart"
    )


async def test_a_configuration_serves_nothing_before_it_is_loaded(
    workspace: Path,
) -> None:
    write(8000)

    config = DynamicConfig(Service, key="svc").file("config.toml")

    assert config.try_current() is None
    assert config.generation == 0

    with pytest.raises(NotInitialisedError):
        config.current()

    await config.init_async()
    assert config.current().port == 8000


# ── Under load ─────────────────────────────────────────────────────────


def test_readers_never_see_a_half_installed_configuration(workspace: Path) -> None:
    """Threads reading while another reloads: every read is self-consistent.

    `name` and `port` are written together and always agree, so a reader
    that sees them disagree has caught an install mid-flight.
    """
    write(1)

    config = DynamicConfig(Service, key="svc").file("config.toml")
    config.init()

    stop = threading.Event()
    torn: list[tuple[str, int]] = []
    reads = [0, 0, 0, 0]

    def read(index: int) -> None:
        while not stop.is_set():
            model = config.current()

            if model.name != f"service-{model.port}":
                torn.append((model.name, model.port))

            reads[index] += 1

    readers = [threading.Thread(target=read, args=(index,)) for index in range(4)]

    for reader in readers:
        reader.start()

    try:
        for port in range(2, 60):
            write(port)
            config.reload()
    finally:
        stop.set()

        for reader in readers:
            reader.join(timeout=10)

    assert not torn, f"a reader saw a half-installed model: {torn[:3]}"
    assert all(count > 0 for count in reads), reads
    assert config.current().port == 59
    assert config.generation == 59, "one install per reload, no more and no fewer"


def test_a_service_polls_a_python_store_while_it_watches_and_serves(
    workspace: Path,
) -> None:
    """The arc a remote store is actually reached through.

    Not `refresh_remote()` on its own: a poller thread reading a Python
    store and reloading, a file watcher running at the same time, and
    four readers throughout — which is where a lock held across a fetch,
    or a store that lost its document to a concurrent file reload, would
    show up. `name` and `port` are written together and always agree, so
    a reader that sees them disagree has caught an install mid-flight.
    """
    write(1)

    class Store(RemoteSource):
        """The store an operator turns, one pool size at a time."""

        def __init__(self) -> None:
            self.pool_size = 10
            self.fetches = 0

        def fetch(self) -> tuple[str, Format]:
            self.fetches += 1
            # A real one blocks here; sleeping is what makes the readers
            # below overlap the fetch rather than tidily follow it.
            time.sleep(0.005)
            return json.dumps({"svc": {"pool_size": self.pool_size}}), Format.JSON

        def describe(self) -> str:
            return "the pool-size service"

    def write_atomically(port: int) -> None:
        """`write`, but the watcher can never see a truncated file.

        `Path.write_text` truncates and then writes, and an empty file is
        *valid* TOML — so a watcher that fires in that window installs a
        model of pure defaults, which reads as torn here and has nothing
        to do with the remote store. A rename is one step to a poller.
        """
        write(port, path="config.toml.tmp")
        Path("config.toml.tmp").replace("config.toml")

    store = Store()
    config = DynamicConfig(Service, key="svc").file("config.toml").remote(store)
    config.refresh_remote()
    config.init()

    assert config.current().pool_size == 10

    stop = threading.Event()
    torn: list[tuple[str, int]] = []
    reads = [0, 0, 0, 0]

    def read(index: int) -> None:
        while not stop.is_set():
            model = config.current()

            if model.name != f"service-{model.port}":
                torn.append((model.name, model.port))

            reads[index] += 1

    readers = [threading.Thread(target=read, args=(index,)) for index in range(4)]
    for reader in readers:
        reader.start()

    def poll() -> None:
        for size in range(11, 31):
            store.pool_size = size
            config.refresh_remote()
            config.reload()

    poller = threading.Thread(target=poll)

    try:
        with config.watch(debounce=0.01, poll_interval=0.01):
            poller.start()

            for port in range(2, 40):
                write_atomically(port)
                config.reload()

            poller.join(timeout=60)
    finally:
        stop.set()

        for reader in readers:
            reader.join(timeout=10)

    assert not poller.is_alive()
    assert not torn, f"a reader saw a half-installed model: {torn[:3]}"
    assert all(count > 0 for count in reads), reads
    # One at startup and one per poll, and not one for any of the ~58
    # loads the watcher and the writer drove in between: a load merges the
    # document last fetched and touches no network.
    assert store.fetches == 21, "a load must not fetch; only the poller does"

    # The store's last word survives everything the file watcher did, and
    # the two caches still agree.
    config.reload()

    assert config.current().pool_size == 30
    assert config.current() is config._core.current()
    assert config.source_of("pool_size") == Origin(
        kind="remote", detail="the pool-size service"
    )


async def test_many_services_share_a_loop_without_interfering(
    workspace: Path,
) -> None:
    """Eight configurations, eight watchers, one loop."""
    for index in range(8):
        write(index + 1, path=f"service-{index}.toml")

    configs = [
        DynamicConfig(Service, key="svc").file(f"service-{index}.toml")
        for index in range(8)
    ]

    await asyncio.gather(*(config.init_async() for config in configs))

    assert [config.current().port for config in configs] == list(range(1, 9))

    watches = await asyncio.gather(
        *(config.watch_async(debounce=0.05, poll_interval=0.05) for config in configs)
    )

    try:
        # Edit one of them; the other seven must not move.
        target = configs[3]
        deadline = asyncio.get_running_loop().time() + 20

        while (
            target.current().port != 400
            and asyncio.get_running_loop().time() < deadline
        ):
            write(400, path="service-3.toml")
            await asyncio.sleep(0.1)

        assert target.current().port == 400
        assert [config.current().port for config in configs] == [
            1,
            2,
            3,
            400,
            5,
            6,
            7,
            8,
        ]
        assert all(config.generation == 1 for config in configs if config is not target)
    finally:
        for watch in watches:
            watch.stop()


# ── Under a framework ──────────────────────────────────────────────────


def test_the_fastapi_example_serves_reloads_and_overrides(workspace: Path) -> None:
    """The shipped FastAPI example, driven the way a test suite would.

    This also exercises the async startup handler, which is where
    `watch_async` earns its place: the loop starting the app is the loop
    that will answer requests.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    import importlib

    from fastapi.testclient import TestClient

    module = importlib.import_module("10_fastapi_service")

    path = workspace / "config.toml"
    path.write_text('[db]\nhost = "db.internal"\npool_size = 16\n')

    application, config, dependency = module.build(path)

    with TestClient(application) as client:
        assert client.get("/async/health").json() == {
            "style": "async",
            "host": "db.internal",
            "pool": 16,
        }
        assert client.get("/sync/health").json() == {
            "style": "sync",
            "host": "db.internal",
            "pool": 16,
        }

        # A deployment edits the file; both endpoint styles follow.
        path.write_text('[db]\nhost = "db.replica"\npool_size = 64\n')
        config.reload()

        for style in ("async", "sync"):
            body = client.get(f"/{style}/health").json()
            assert body["host"] == "db.replica", body
            assert body["pool"] == 64, body

        # Diagnostics are an endpoint, and they name the source.
        explained = client.get("/async/explain").json()
        assert explained["path"] == "pool_size"
        assert "64" in explained["explanation"]

        # A test overrides the dependency rather than the configuration.
        application.dependency_overrides[dependency] = lambda: module.Database(
            host="fixture", pool_size=1
        )

        assert client.get("/sync/health").json()["host"] == "fixture"

        application.dependency_overrides.clear()
        assert client.get("/sync/health").json()["host"] == "db.replica"

    # Client after client runs the lifespan again — what a test suite does
    # dozens of times, and what `uvicorn --reload` does on every edit.
    # Each exit stops that run's watcher, so the next one starts cleanly
    # rather than meeting `AlreadyExists`. Pairing the start with the stop
    # in one lifespan is what makes that true; two separate handlers had
    # to be kept idempotent by hand.
    for _ in range(3):
        with TestClient(application) as client:
            assert client.get("/sync/health").json()["host"] == "db.replica"


def test_the_flask_example_serves_and_reloads(workspace: Path) -> None:
    """The same arc under a synchronous framework, where every read is a thread."""
    pytest.importorskip("flask")

    import importlib

    module = importlib.import_module("11_flask_service")

    path = workspace / "config.toml"
    path.write_text('[db]\nhost = "db.internal"\nport = 5432\npool_size = 16\n')

    application, config = module.build(path)

    with application.test_client() as client:
        assert client.get("/health").get_json() == {
            "host": "db.internal",
            "port": 5432,
            "pool": 16,
        }

        path.write_text('[db]\nhost = "db.replica"\nport = 6543\npool_size = 64\n')
        config.reload()

        assert client.get("/health").get_json() == {
            "host": "db.replica",
            "port": 6543,
            "pool": 64,
        }

    # Several requests in flight while reloads land. `host` and `port` are
    # written together from here on, so a response where they disagree is
    # a request that saw two configurations.
    #
    # Outside the `with`, and a client per thread: Flask's test client
    # keeps its context in a context variable, and a block entered on one
    # thread cannot be left on another. The concurrency under test is the
    # configuration's, not the test client's.
    def deploy(port: int) -> None:
        path.write_text(f'[db]\nhost = "db-{port}"\nport = {port}\npool_size = 64\n')
        config.reload()

    deploy(7000)

    results: list[dict[str, object]] = []
    collect = threading.Lock()

    def hammer() -> None:
        client = application.test_client()
        seen = [client.get("/health").get_json() for _ in range(50)]

        with collect:
            results.extend(seen)

    threads = [threading.Thread(target=hammer) for _ in range(3)]

    for thread in threads:
        thread.start()

    for port in range(7001, 7021):
        deploy(port)

    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 150

    for body in results:
        assert body["host"] == f"db-{body['port']}", body


def test_django_settings_carry_a_reloadable_configuration(workspace: Path) -> None:
    """Django's settings are frozen at boot; this is the half that is not."""
    pytest.importorskip("django")

    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], DATABASES={})

    path = workspace / "config.toml"
    path.write_text('[db]\nname = "django"\nport = 5432\n')

    runtime = DynamicConfig(Service, key="db").file(str(path))
    runtime.init()
    settings.RUNTIME = runtime

    def view() -> dict[str, object]:
        db = settings.RUNTIME.current()

        return {"name": db.name, "port": db.port}

    assert view() == {"name": "django", "port": 5432}

    path.write_text('[db]\nname = "django"\nport = 6543\n')
    runtime.reload()

    assert view() == {"name": "django", "port": 6543}, "the view served a stale model"

    # A bad edit leaves the view answering exactly what it answered before.
    path.write_text('[db]\nname = "django"\nport = 0\n')

    with pytest.raises(InvalidError):
        runtime.reload()

    assert view() == {"name": "django", "port": 6543}

    del settings.RUNTIME


# ── Timing, as a property rather than a benchmark ──────────────────────


async def test_a_reload_does_not_stall_the_loop_answering_requests(
    workspace: Path,
) -> None:
    """What the async twins are for, measured the way a service feels it.

    A loop tick that should take a millisecond must not take a hundred
    because the process happened to be reloading. The bound is loose on
    purpose — this asserts the work is off the loop, not how fast the
    machine is.
    """
    write(1)

    config = DynamicConfig(Service, key="svc").file("config.toml").cache(".cache.json")
    await config.init_async()

    lateness: list[float] = []

    async def tick() -> None:
        loop = asyncio.get_running_loop()

        while True:
            started = loop.time()
            await asyncio.sleep(0.001)
            lateness.append(loop.time() - started - 0.001)

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.05)
    lateness.clear()

    # A wall-clock window rather than a reload count, so the tick budget
    # is the same on a fast machine and a loaded CI runner.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 0.5
    port = 1

    while loop.time() < deadline:
        port += 1
        write(port)
        await config.reload_async()

    watch = await config.watch_async(debounce=0.25, poll_interval=0.5)
    ticker.cancel()
    watch.stop()

    # How *many* ticks landed is the scheduler's business, and on a loaded
    # runner it is not a fact about this library. That each one that did
    # land was not held up for a tenth of a second is.
    assert port > 1, "no reload ran"
    assert lateness, "the loop did not run at all during the reloads"
    assert max(lateness) < 0.5, f"the loop stalled for {max(lateness):.3f}s"
    assert config.current().port == port
