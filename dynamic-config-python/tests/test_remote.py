"""A remote store written in Python, on the fetch path.

The engine calls `fetch()` on whichever thread asked for the refresh, so
these are as much about the GIL and about locks as they are about the
values that come back: a fetch that stops the process, or one that
deadlocks when it reads the configuration it is fetching for, would be
this binding's own doing rather than a user's mistake.
"""

from __future__ import annotations

import gc
import json
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path

import pytest

from dynamic_config import (
    AuthError,
    BackendError,
    DynamicConfig,
    Format,
    RemoteError,
    RemoteSource,
)


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432


class Store(RemoteSource):
    """A store that answers with whatever it was given."""

    def __init__(self, document: str, fmt: object = Format.JSON) -> None:
        self.document = document
        self.format = fmt
        self.fetches = 0
        self.describes = 0

    def fetch(self) -> tuple[str, Format]:
        self.fetches += 1
        return self.document, self.format  # type: ignore[return-value]

    def describe(self) -> str:
        self.describes += 1
        return "the test store"


def document(host: str = "remote", port: int = 6000) -> str:
    return json.dumps({"db": {"host": host, "port": port}})


# ── The document reaches the model ─────────────────────────────────────


def test_a_python_store_supplies_the_configuration(workspace: Path) -> None:
    config = DynamicConfig(Database, key="db").remote(Store(document()))
    config.refresh_remote()

    assert config.init_and_current() == Database(host="remote", port=6000)


def test_provenance_says_what_describe_said(workspace: Path) -> None:
    config = DynamicConfig(Database, key="db").remote(Store(document()))
    config.refresh_remote()
    config.init()

    origin = config.source_of("port")

    assert origin is not None
    assert origin.kind == "remote"
    assert origin.detail == "the test store"
    assert config.remote_description == "the test store"


def test_describe_is_asked_once_at_install_not_per_load(workspace: Path) -> None:
    # The engine reads `describe()` on the *load* path, for provenance. If
    # that reached Python, every load of every configuration with a remote
    # source would re-enter the interpreter — from a watcher thread as
    # often as from a caller's.
    store = Store(document())
    config = DynamicConfig(Database, key="db").remote(store)
    config.refresh_remote()

    for _ in range(5):
        config.reload()
        config.source_of("port")
        config.explain("port")

    assert store.describes == 1


def test_nothing_is_fetched_until_refresh_remote(workspace: Path) -> None:
    store = Store(document())
    config = DynamicConfig(Database, key="db").remote(store)
    config.init()

    assert store.fetches == 0
    assert config.current() == Database()

    config.refresh_remote()
    config.reload()

    assert store.fetches == 1
    assert config.current().host == "remote"


def test_a_load_does_not_re_fetch(workspace: Path) -> None:
    store = Store(document())
    config = DynamicConfig(Database, key="db").remote(store)
    config.refresh_remote()

    for _ in range(4):
        config.reload()

    assert store.fetches == 1, "a load must touch no network"


def test_the_remote_layer_sits_above_files_and_below_the_environment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("config.toml").write_text('[db]\nhost = "from-the-file"\nport = 1\n')

    config = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .env("DCTEST_")
        .remote(Store(document(host="from-the-store", port=2)))
    )
    config.refresh_remote()
    config.init()

    assert config.current().host == "from-the-store", "remote beats a file"

    monkeypatch.setenv("DCTEST_DB_HOST", "from-the-environment")
    config.reload()

    assert config.current().host == "from-the-environment", "the machine wins"


@pytest.mark.parametrize(
    ("text", "fmt"),
    [
        ('{"db": {"host": "h"}}', Format.JSON),
        ('[db]\nhost = "h"\n', Format.TOML),
        ("db:\n  host: h\n", Format.YAML),
        ('{"db": {"host": "h"}}', "json"),
    ],
)
def test_every_format_a_store_may_answer_in(
    workspace: Path, text: str, fmt: object
) -> None:
    config = DynamicConfig(Database, key="db").remote(Store(text, fmt))
    config.refresh_remote()

    assert config.init_and_current().host == "h"


# ── A fetch that fails ─────────────────────────────────────────────────


LEAKY = "https://store.internal/v1/kv?token=hunter2-do-not-log"


class Broken(RemoteSource):
    """A store whose exception carries a credential, as they do."""

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure or RuntimeError(f"GET {LEAKY} returned 500")

    def fetch(self) -> tuple[str, Format]:
        raise self.failure

    def describe(self) -> str:
        return "the broken store"


def test_a_raising_fetch_arrives_as_remote_error(workspace: Path) -> None:
    config = DynamicConfig(Database, key="db").remote(Broken())

    with pytest.raises(RemoteError) as raised:
        config.refresh_remote()

    assert raised.value.kind == "remote"


def test_the_message_of_a_failing_fetch_is_not_repeated(workspace: Path) -> None:
    config = DynamicConfig(Database, key="db").remote(Broken())

    with pytest.raises(RemoteError) as raised:
        config.refresh_remote()

    failure = raised.value

    # The rule this repository keeps: a store's exception routinely carries
    # the URL it called, and a URL routinely carries a token.
    for rendering in (str(failure), repr(failure)):
        assert "hunter2-do-not-log" not in rendering
        assert "token=" not in rendering
        assert LEAKY not in rendering

    # What is kept is the exception's *type*, which is what a person reads.
    assert "RuntimeError" in str(failure)


def test_the_exception_the_source_raised_is_attached_as_the_cause(
    workspace: Path,
) -> None:
    # Scrubbing the message must not mean losing the traceback: Python's
    # own place for "what really went wrong" is `__cause__`, and that is
    # where a debugger and a `logging.exception` both look.
    config = DynamicConfig(Database, key="db").remote(Broken())

    with pytest.raises(RemoteError) as raised:
        config.refresh_remote()

    cause = raised.value.__cause__

    assert isinstance(cause, RuntimeError)
    assert LEAKY in str(cause)


def test_a_source_that_raises_auth_error_keeps_the_distinction(
    workspace: Path,
) -> None:
    # `Remote` may fix itself by waiting; `Auth` will not. A Python source
    # that reads its own 401 says so by raising the exception that means it.
    config = DynamicConfig(Database, key="db").remote(
        Broken(AuthError("the token was refused"))
    )

    with pytest.raises(AuthError) as raised:
        config.refresh_remote()

    assert raised.value.kind == "auth"


def test_a_failed_fetch_does_not_poison_the_configuration(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "from-the-file"\nport = 1\n')

    good = Store(document())
    config = DynamicConfig(Database, key="db").file("config.toml").remote(good)
    config.refresh_remote()
    config.init()

    assert config.current().host == "remote"

    # The store has a bad afternoon...
    config.remote(Broken())

    with pytest.raises(RemoteError):
        config.refresh_remote()

    # ...the installed model still serves, and a reload still works: a
    # failed *fetch* is not a failed load.
    assert config.current().host == "remote"
    config.reload()
    assert config.current().host == "from-the-file"

    # ...and the store comes back.
    config.remote(good)
    config.refresh_remote()
    config.reload()

    assert config.current().host == "remote"
    assert config.generation > 1


def test_a_failing_fetch_leaves_the_previous_document_in_place(
    workspace: Path,
) -> None:
    class Flaky(RemoteSource):
        def __init__(self) -> None:
            self.answered = False

        def fetch(self) -> tuple[str, Format]:
            if self.answered:
                raise RuntimeError("the store went away")

            self.answered = True
            return document(), Format.JSON

        def describe(self) -> str:
            return "a store that answers once"

    config = DynamicConfig(Database, key="db").remote(Flaky())
    config.refresh_remote()
    config.init()

    with pytest.raises(RemoteError):
        config.refresh_remote()

    config.reload()

    assert config.current().host == "remote", (
        "a failed refresh must not drop the document the last good one kept"
    )


def test_a_keyboard_interrupt_from_a_fetch_is_not_swallowed(workspace: Path) -> None:
    # Ctrl-C during a long fetch is the interpreter talking, not the store.
    # Re-raising it as `RemoteError` would make a hung fetch the one thing
    # Python guarantees is interruptible and then is not.
    config = DynamicConfig(Database, key="db").remote(Broken(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        config.refresh_remote()


def test_refreshing_with_no_source_says_which_call_is_missing(
    workspace: Path,
) -> None:
    config = DynamicConfig(Database, key="db")

    with pytest.raises(RemoteError, match="no remote source"):
        config.refresh_remote()


# ── What `fetch()` is allowed to answer ────────────────────────────────


class Answering(RemoteSource):
    """A store answering exactly what it was constructed with."""

    def __init__(self, answer: object) -> None:
        self.answer = answer

    def fetch(self) -> tuple[str, Format]:
        return self.answer  # type: ignore[return-value]

    def describe(self) -> str:
        return "a store with opinions"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("just a string", r"must return a \(text, format\) pair"),
        ((b"bytes", Format.JSON), "must be a str"),
        (("{}", "csv"), "is not a document format"),
        (("{}", 3), "must be a Format"),
    ],
)
def test_a_fetch_that_breaks_the_contract_says_so(
    workspace: Path, answer: object, expected: str
) -> None:
    config = DynamicConfig(Database, key="db").remote(Answering(answer))

    with pytest.raises(RemoteError, match=expected):
        config.refresh_remote()


def test_a_contract_failure_names_a_type_and_never_the_value(
    workspace: Path,
) -> None:
    config = DynamicConfig(Database, key="db").remote(
        Answering(("a-document-nobody-should-see", "csv"))
    )

    with pytest.raises(RemoteError) as raised:
        config.refresh_remote()

    assert "a-document-nobody-should-see" not in str(raised.value)


def test_a_missing_method_is_a_class_definition_error(workspace: Path) -> None:
    class Halfway(RemoteSource):  # type: ignore[misc]
        def describe(self) -> str:
            return "half a store"

    with pytest.raises(TypeError, match="fetch"):
        Halfway()  # type: ignore[abstract]


def test_an_object_that_is_not_a_source_is_refused_at_the_call(
    workspace: Path,
) -> None:
    config = DynamicConfig(Database, key="db")

    with pytest.raises(TypeError, match="fetch"):
        config.remote(object())  # type: ignore[arg-type]


def test_a_describe_that_is_not_a_string_is_refused_at_the_call(
    workspace: Path,
) -> None:
    class Nameless(RemoteSource):
        def fetch(self) -> tuple[str, Format]:
            return document(), Format.JSON

        def describe(self) -> str:
            return 42  # type: ignore[return-value]

    config = DynamicConfig(Database, key="db")

    with pytest.raises(TypeError, match="describe"):
        config.remote(Nameless())


# ── Swapping and clearing ──────────────────────────────────────────────


def test_swapping_the_source_drops_the_previous_document(workspace: Path) -> None:
    config = DynamicConfig(Database, key="db").remote(Store(document()))
    config.refresh_remote()
    config.init()

    assert config.current().host == "remote"

    # A new source answering with the old store's values would be a puzzle
    # nobody needs — the engine's rule, kept across the binding.
    config.remote(Store(document(host="second", port=7)))
    config.reload()

    assert config.current() == Database()

    config.refresh_remote()
    config.reload()

    assert config.current().host == "second"


def test_clear_remote_drops_the_document_and_keeps_the_source(
    workspace: Path,
) -> None:
    config = DynamicConfig(Database, key="db").remote(Store(document()))
    config.refresh_remote()
    config.init()

    config.clear_remote()
    config.reload()

    assert config.current() == Database()
    assert config.remote_description == "the test store"

    config.refresh_remote()
    config.reload()

    assert config.current().host == "remote"


# ── The last known good ────────────────────────────────────────────────


def test_the_cache_recovers_a_document_a_python_store_supplied(
    workspace: Path,
) -> None:
    first = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("last.json", "full")
        .remote(Store(document(host="planted-host", port=7)))
    )
    first.refresh_remote()
    first.init()

    assert json.loads(Path("last.json").read_text())

    # The store is gone and the file is broken: recovery is the whole
    # point, and it must not care which layer the values came from.
    Path("config.toml").write_text("[db\nthis is not toml")

    second = (
        DynamicConfig(Database, key="db").file("config.toml").cache("last.json", "full")
    )
    second.init()

    assert second.current().host == "planted-host"
    assert second.current().port == 7


def test_a_failed_fetch_never_reaches_the_cache(workspace: Path) -> None:
    config = (
        DynamicConfig(Database, key="db").cache("last.json", "full").remote(Broken())
    )

    with pytest.raises(RemoteError):
        config.refresh_remote()

    assert not Path("last.json").exists(), (
        "a fetch that failed installed nothing, so there is no known good"
    )


# ── The GIL, and re-entrancy ───────────────────────────────────────────


class Sleeping(RemoteSource):
    """A store that takes 200 ms in the way a network call does."""

    def fetch(self) -> tuple[str, Format]:
        time.sleep(0.2)
        return document(), Format.JSON

    def describe(self) -> str:
        return "a slow store"


def _ticks_during(work: object) -> int:
    """How far a second Python thread gets while ``work`` runs."""
    counter = 0
    stop = threading.Event()

    def tick() -> None:
        nonlocal counter
        while not stop.is_set():
            counter += 1

    ticker = threading.Thread(target=tick)
    ticker.start()
    try:
        work()  # type: ignore[operator]
    finally:
        stop.set()
        ticker.join()

    return counter


def test_a_python_fetch_does_not_stop_other_threads(workspace: Path) -> None:
    """The measurement this whole design turns on.

    The note that preceded this item assumed a Python object on the fetch
    path meant *the GIL is held for the length of an HTTP request, and
    every other thread in the process stops*, and proposed a worker thread
    and a channel to avoid it. Measured, that is not what happens: a
    `fetch()` doing I/O releases the GIL itself, exactly as any other
    Python thread does, so a second thread keeps running at very nearly
    its free rate. The threshold below is deliberately generous — the
    failure this guards against is a *stopped* thread, which measures near
    zero, not a slightly slower one.
    """
    baseline = _ticks_during(lambda: time.sleep(0.2))

    config = DynamicConfig(Database, key="db").remote(Sleeping())
    during = _ticks_during(config.refresh_remote)

    assert during > baseline * 0.25, (
        f"a second thread managed {during} ticks while a 200 ms fetch ran, "
        f"against {baseline} with the same sleep and no fetch: the GIL is "
        "being held across the fetch"
    )


def test_another_thread_can_read_while_a_fetch_is_in_flight(
    workspace: Path,
) -> None:
    """No Rust lock is held across the fetch.

    Which is what makes the rest of this file possible: a reload, a
    snapshot and a read all have to work while a store is being read.
    """
    started = threading.Event()
    release = threading.Event()

    class Parked(RemoteSource):
        def fetch(self) -> tuple[str, Format]:
            started.set()
            release.wait(timeout=30)
            return document(), Format.JSON

        def describe(self) -> str:
            return "a parked store"

    Path("config.toml").write_text('[db]\nhost = "from-the-file"\nport = 1\n')
    config = DynamicConfig(Database, key="db").file("config.toml").remote(Parked())
    config.init()

    refresher = threading.Thread(target=config.refresh_remote)
    refresher.start()

    assert started.wait(timeout=10), "the fetch never started"

    # Everything a reader or a watcher would do, with a fetch parked.
    assert config.current().host == "from-the-file"
    assert config.snapshot().to_dict()["host"] == "from-the-file"
    assert config.explain("host").winner is not None
    config.reload()

    release.set()
    refresher.join(timeout=30)

    assert not refresher.is_alive()
    config.reload()

    assert config.current().host == "remote"


def test_a_fetch_may_read_the_configuration_it_is_fetching_for(
    workspace: Path,
) -> None:
    seen: list[object] = []

    class Curious(RemoteSource):
        def __init__(self) -> None:
            self.config: DynamicConfig[Database] | None = None

        def fetch(self) -> tuple[str, Format]:
            assert self.config is not None
            seen.append(self.config.try_current())
            seen.append(self.config.snapshot().to_dict())
            seen.append(self.config.generation)
            return document(), Format.JSON

        def describe(self) -> str:
            return "a curious store"

    source = Curious()
    config = DynamicConfig(Database, key="db").remote(source)
    source.config = config
    config.init()

    # A test-level deadline rather than a hang: a regression here has to
    # read as a failure rather than as a suite that never finishes.
    done = threading.Event()

    def refresh() -> None:
        config.refresh_remote()
        done.set()

    worker = threading.Thread(target=refresh, daemon=True)
    worker.start()

    assert done.wait(timeout=30), "a fetch that read its configuration hung"
    assert len(seen) == 3


def test_a_fetch_that_refreshes_again_is_refused_rather_than_recursing(
    workspace: Path,
) -> None:
    caught: list[BaseException] = []

    class Recursive(RemoteSource):
        def __init__(self) -> None:
            self.config: DynamicConfig[Database] | None = None

        def fetch(self) -> tuple[str, Format]:
            assert self.config is not None
            try:
                self.config.refresh_remote()
            except BaseException as failure:
                caught.append(failure)
            return document(), Format.JSON

        def describe(self) -> str:
            return "a recursive store"

    source = Recursive()
    config = DynamicConfig(Database, key="db").remote(source)
    source.config = config

    done = threading.Event()

    def refresh() -> None:
        config.refresh_remote()
        done.set()

    worker = threading.Thread(target=refresh, daemon=True)
    worker.start()

    assert done.wait(timeout=30), "a fetch that refreshed again hung"
    assert len(caught) == 1
    assert isinstance(caught[0], BackendError)
    assert "fetch()" in str(caught[0])

    # And the refusal is per-thread and does not stick: the next refresh
    # on this thread works.
    config.refresh_remote()
    config.init()

    assert config.current().host == "remote"


# ── Lifetimes ──────────────────────────────────────────────────────────


def test_a_source_holding_its_configuration_is_still_collected(
    workspace: Path,
) -> None:
    """The cycle every real source closes.

    A `fetch()` that reads the configuration it feeds holds it, and the
    configuration holds the source — through a `#[pyclass]` and, before
    this, through a leaked `static` the collector could never reach. The
    edge is reported from `__traverse__` and dropped by `__clear__`, so
    the cycle collects like any other.
    """

    class Circular(RemoteSource):
        def __init__(self, config: DynamicConfig[Database]) -> None:
            self.config = config

        def fetch(self) -> tuple[str, Format]:
            return document(), Format.JSON

        def describe(self) -> str:
            return "a circular store"

    config = DynamicConfig(Database, key="db")
    source = Circular(config)
    config.remote(source)
    config.refresh_remote()
    config.init()

    gone = weakref.ref(source)
    del config, source
    gc.collect()

    assert gone() is None, "a source that holds its configuration leaked"


def test_a_dropped_configuration_leaves_no_dangling_fetch(workspace: Path) -> None:
    # The shim the engine holds lives in a leaked `static`; the object it
    # calls does not. Once the configuration is gone the shim has nothing
    # to call, and it must say so rather than reach for freed memory.
    store = Store(document())
    config = DynamicConfig(Database, key="db").remote(store)
    config.refresh_remote()

    del config
    gc.collect()

    assert store.fetches == 1


# ── Async ──────────────────────────────────────────────────────────────


async def test_refresh_remote_async_reads_the_store(workspace: Path) -> None:
    store = Store(document())
    config = DynamicConfig(Database, key="db").remote(store)

    await config.refresh_remote_async()
    await config.init_async()

    assert config.current().host == "remote"
    assert store.fetches == 1


async def test_refresh_remote_async_reports_a_failure_the_same_way(
    workspace: Path,
) -> None:
    config = DynamicConfig(Database, key="db").remote(Broken())

    with pytest.raises(RemoteError) as raised:
        await config.refresh_remote_async()

    assert "hunter2-do-not-log" not in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)
