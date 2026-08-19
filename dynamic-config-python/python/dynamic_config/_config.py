"""The configuration object: a model, where its values live, and its lifecycle.

Sources are chosen fluently and take effect at the first load; everything
after that — install, reload, watch, hooks, diagnostics — mirrors the Rust
crate one call at a time. The read path deliberately never crosses back
into Rust: each installed model is published onto this object by a hook,
so `current()` is an attribute lookup.


Maintainer's note, 0.7 review: this file is large because `DynamicConfig`
is large — 1,500 of these lines are one cohesive generic class, and the
0.7 maintainability pass looked hard at splitting it. The verdict was no:
every candidate cut (lifecycle / reads / hooks / async) shares the same
lock, the same `_core` handle and the same invariants, so a split means
mixins whose halves cannot be understood alone — more files, more
indirection, the same coupling. The dispatchers and `_Gate` at the bottom
are the parts that genuinely stand alone, and they are already at the
bottom for that reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import warnings
import weakref
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import Executor
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import (
    Any,
    Callable,
    Generic,
    TypeVar,
)

from . import _core
from ._diagnostics import (
    Change,
    Contribution,
    Explanation,
    Origin,
    Report,
    Resolved,
    Snapshot,
    UnknownKey,
    changed_paths,
)
from ._dispatch import Backpressure, Dispatch
from ._errors import NotInitialisedError
from ._events import ConfigEvent, Reloaded, ReloadFailed
from ._executor import default_executor
from ._lifetime import _LIVE_CONFIGS, HookGuard, Watch, _register
from ._notify import EventNotifier, Notifier
from ._remote import AsyncRemoteSource, RemoteSource, _AwaitedDocument
from ._schema import is_values_type, schema_for
from ._settings import (
    _SETTINGS_SOURCING,
    _as_paths,
    _declared_sourcing,
    _is_settings,
    _leaf_paths,
)
from ._telemetry import ConfigStatus, RemoteStatus

M = TypeVar("M")


def _refusal(status: ConfigStatus, consecutive: int) -> ReloadFailed:
    """The `ReloadFailed` for whatever the last refusal was."""
    return ReloadFailed(
        generation=int(status.generation),
        kind=status.last_failure.kind if status.last_failure else "unknown",
        path=status.last_failure.path if status.last_failure else "",
        consecutive=consecutive,
    )


def _touches(wanted: frozenset[str], moved: Sequence[Change]) -> bool:
    """Whether any changed path is one of ``wanted``, or sits under one.

    A path naming a table covers what is inside it: asking about ``pool``
    is asking about ``pool.max_size`` too, because a caller who names a
    section means the section.
    """
    return any(
        change.path in wanted
        or any(change.path.startswith(f"{name}.") for name in wanted)
        for change in moved
    )


class DynamicConfig(Generic[M]):
    """One configuration: a declared class, and where its values live.

    Sources are chosen fluently and take effect at the first load; the
    lifecycle after that mirrors the Rust crate one for one.
    """

    __slots__ = (
        "__weakref__",
        "_async_remote",
        "_cached",
        "_core",
        "_event_notifier_instance",
        "_executor",
        "_model",
        "_notifier_instance",
        "_overrides",
        "_schema",
    )

    def __init__(
        self,
        model: type[M],
        key: str,
        *,
        executor: Executor | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        """Builds a configuration for ``model`` under the section ``key``.

        Parameters:
            model: the class this configuration resolves to — a Pydantic
                model, a plain `dataclasses.dataclass`, a
                `msgspec.Struct`, or :class:`~dynamic_config.Values` for
                a configuration with no schema at all. Anything else is a
                `TypeError` here, where the mistake is on screen.
            key: the section key. Every file's top-level keys are
                sections, so this is which one is yours; it also names
                the environment prefix (``env("APP_")`` reads
                ``APP_{KEY}_*``), the cache entry and every diagnostic.
                Pass ``""`` for a configuration with nothing to call
                itself, alongside :meth:`whole_document`.
            executor: which thread pool pays for the blocking half of the
                ``_async`` calls. ``None`` — the default — follows
                :func:`~dynamic_config.set_executor`, which in turn
                follows the running loop's own.
            secrets: dotted paths whose values must never reach a
                diagnostic, for a schemaless configuration. A declared
                model already says which of its fields are secret —
                `SecretStr`, ``field(metadata={"secret": True})``, or
                ``msgspec.Meta(extra={"secret": True})`` — and these are
                **added** to that rather than replacing it.

        Nothing is read here: sources are chosen with the fluent methods
        and take effect at the first load.
        """
        # Whatever kind of schema this is — a Pydantic model, a plain
        # dataclass, a msgspec Struct — the adapter answers the same
        # three questions and nothing below here knows the difference. A
        # class this package cannot read is refused right at the door.
        schema = schema_for(model, secrets)

        # A settings class that declares where to read from would get none
        # of it here: `model_validate` does not run pydantic-settings'
        # sources, and this engine is the source. Say so rather than
        # letting an `env_prefix` look configured and do nothing.
        if _is_settings(model):
            declared = _declared_sourcing(model)

            if declared:
                warnings.warn(
                    f"{model.__name__} declares "
                    f"{', '.join(sorted(declared))} in its "
                    "SettingsConfigDict; DynamicConfig is the source here, "
                    "so none of that is read. Use "
                    "DynamicConfig.from_settings(...) to translate what "
                    "can be translated into engine sources, or declare the "
                    "sources on the configuration instead.",
                    UserWarning,
                    stacklevel=2,
                )

        self._model = model
        self._cached: M | None = None
        # A mirror of the engine's override layer, in the order it was
        # set. The engine owns the layer and has no way to read it back,
        # and `overrides()` has to *restore* what it found rather than
        # empty it — otherwise a nested `with` would drop the outer
        # block's pin on the way out of the inner one.
        self._overrides: dict[str, Any] = {}
        # `None` means "whatever `set_executor` says", which in turn
        # means "the loop's own" unless somebody said otherwise.
        self._executor = executor
        self._schema = schema
        # A declared model's own secret fields, plus whatever the caller
        # named — and `None` when nobody could say. An empty list is the
        # knowledge that there are no secrets; a schemaless configuration
        # that named none has no such knowledge, and the engine goes on
        # refusing a redacting cache until `secrets=` supplies it.
        secret_paths = sorted({*schema.secret_paths(), *secrets})
        known = secret_paths if secret_paths or not is_values_type(model) else None

        self._core = _core.Config(
            schema.validate,
            key,
            known,
            schema.field_names(),
        )

        # The read path, and the reason it is cheap: the engine publishes
        # into a Python attribute through this hook, so `current()` never
        # crosses back into Rust — a boundary crossing costs roughly ten
        # attribute lookups, and configuration is read on every request.
        #
        # A weak reference rather than `self`: the hook lives inside the
        # Rust object this one owns, and a strong one would be a cycle
        # through a type Python's collector cannot traverse.
        weak = weakref.ref(self)

        def publish(_previous: M | None, current: M) -> None:
            """Copies each installed model onto this object, for `current()`."""
            target = weak()

            if target is not None:
                target._cached = current

        # Registered before anything else, so a caller's own hook already
        # sees the new value when it asks `current()`.
        self._core.on_reload(publish)

        # The event-loop bridge, built on the first await and not before:
        # a purely synchronous program never starts a notifier thread.
        self._notifier_instance: Notifier | None = None
        self._event_notifier_instance: EventNotifier | None = None
        # The model the last `events()` turn reported, for `changed`.
        # Set by `remote()` when the store is an `AsyncRemoteSource`.
        self._async_remote: _AwaitedDocument | None = None

        _register(_LIVE_CONFIGS, self)

    def _notifier(self, loop: object) -> Notifier:
        """The notifier for this configuration, started on first use.

        One per configuration rather than one per waiter: fifty
        configurations with two consumers each park fifty threads, not a
        hundred, and none of them wakes until something installs.
        """
        del loop  # every loop shares the one notifier; the future carries its own

        if self._notifier_instance is None:
            self._notifier_instance = Notifier(self._core, self.key)

        return self._notifier_instance

    def _event_notifier(self, loop: object) -> EventNotifier:
        """The `events()` twin of :meth:`_notifier`, woken by refusals too.

        Separate on purpose: a configuration whose only consumers are
        `changes()` keeps a thread that refusals never wake.
        """
        del loop

        if self._event_notifier_instance is None:
            self._event_notifier_instance = EventNotifier(self._core, self.key)

        return self._event_notifier_instance

    @classmethod
    def from_settings(
        cls,
        model: type[M],
        key: str,
        *,
        executor: Executor | None = None,
    ) -> DynamicConfig[M]:
        """A configuration whose sources come from a ``BaseSettings`` class.

        pydantic-settings splits into a schema and a set of places to read
        it from. The schema half works here as any model does; the
        sourcing half is what this engine exists to do, and it does not
        run under ``model_validate``. This constructor reads the class's
        ``SettingsConfigDict`` and rebuilds the declaration as engine
        sources, so an existing settings class keeps the variable names
        and files its deployment already sets, and gains layering,
        provenance and hot reload:

        =========================== ====================================
        ``SettingsConfigDict``       becomes
        =========================== ====================================
        ``toml_file``/``json_file``/ :meth:`file`, in that order
        ``yaml_file``
        ``env_file``                 :meth:`env_file`
        ``env_prefix``               a :meth:`bind_env` per leaf field,
                                     so ``APP_HOST`` stays ``APP_HOST``
                                     rather than becoming
                                     ``APP_<KEY>_HOST``
        ``env_nested_delimiter``     the separator inside those names
        ``case_sensitive``           whether they are upper-cased
        =========================== ====================================

        Precedence follows this crate's, which agrees with
        pydantic-settings where the two overlap: files lose to ``.env``,
        which loses to the environment, which loses to overrides.

        ``secrets_dir`` translates too, since the engine grew the source
        it needed: a directory of single-value files, at the same place
        in the order pydantic-settings puts it.

        What cannot be translated is refused rather than dropped:
        ``cli_parse_args`` (a command line is the program's, not a
        configuration's), and an overridden
        ``settings_customise_sources`` (an order this cannot see, let
        alone reproduce). Declare those on the configuration instead, or
        keep the class for its schema and use :class:`DynamicConfig`
        directly.

        Every source is still just a source: chain more onto the result.
        """
        if not _is_settings(model):
            raise TypeError(
                f"{model.__name__} is not a BaseSettings subclass; plain "
                "models declare their sources on the configuration"
            )

        config: Mapping[str, Any] = getattr(model, "model_config", {}) or {}
        refused = [
            name
            for name in ("cli_parse_args",)
            if config.get(name, _SETTINGS_SOURCING[name]) != _SETTINGS_SOURCING[name]
        ]

        if "settings_customise_sources" in vars(model):
            refused.append("settings_customise_sources")

        if refused:
            raise ValueError(
                f"{model.__name__} declares {', '.join(refused)}, which has "
                "no engine equivalent; declare the sources on the "
                "configuration instead of translating this class"
            )

        # Built with the warning suppressed: this constructor is the
        # answer to it, and honouring a declaration is not ignoring it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            configuration = cls(model, key, executor=executor)

        for name in ("toml_file", "json_file", "yaml_file"):
            for path in _as_paths(config.get(name)):
                configuration.file(path)

        for path in _as_paths(config.get("env_file")):
            configuration.env_file(path)

        for path in _as_paths(config.get("secrets_dir")):
            configuration.secrets_dir(path)

        # Unconditionally, rather than only when a prefix was declared: an
        # unprefixed settings class still reads the environment — `HOST`,
        # `PORT`, `DATABASE_URL` — and that is the *common* shape. Only the
        # name each binding looks for depends on the prefix.
        prefix = config.get("env_prefix", "")
        delimiter = config.get("env_nested_delimiter") or "_"
        upper = not config.get("case_sensitive", False)

        for segments in _leaf_paths(model):
            variable = f"{prefix}{delimiter.join(segments)}"
            configuration.bind_env(
                ".".join(segments), variable.upper() if upper else variable
            )

        return configuration

    # ── Sources ────────────────────────────────────────────────────────

    @property
    def key(self) -> str:
        """The section key this configuration reads."""
        return self._core.key

    @property
    def model(self) -> type[M]:
        """The Pydantic model this configuration validates against."""
        return self._model

    def file(self, path: str) -> DynamicConfig[M]:
        """Adds a configuration file. Merged in call order; later wins.

        Parameters:
            path: the file to read. The format comes from the extension —
                ``.json``, ``.toml``, ``.yaml``/``.yml`` — and a file
                that is not there is skipped, which is what makes an
                optional secrets file work.
        """
        self._core.file(str(path))
        return self

    def discover(self, name: str, paths: Iterable[str]) -> DynamicConfig[M]:
        """Looks for ``{name}.{ext}`` in each directory.

        Every directory that has a match contributes one layer, so the
        search order *is* the layering order — and all of them sit below
        anything :meth:`file` listed, because a listed file is a
        deliberate statement and a search result is a guess about the
        machine.

        Parameters:
            name: the stem to look for. ``config`` finds ``config.toml``,
                ``config.json`` or ``config.yaml``.
            paths: the directories to look in, in order.
        """
        self._core.discover(name, [str(path) for path in paths])
        return self

    def env(self, prefix: str) -> DynamicConfig[M]:
        """The environment layer, read above every file.

        Parameters:
            prefix: the variable prefix, trailing underscore included.
                It combines with the section key, so ``env("APP_")`` on a
                ``db`` configuration reads ``APP_DB_*``; with ``key=""``
                it is the prefix alone.
        """
        self._core.env(prefix)
        return self

    def nest(self, separator: str) -> DynamicConfig[M]:
        """The separator that means nesting inside a variable name.

        Parameters:
            separator: what introduces one level. ``__`` unless given, so
                ``APP_DB_POOL__MAX_SIZE`` is ``pool.max_size``. A single
                separator cannot mean both "word break" and "nesting", so
                whatever this is must be something a field name will not
                contain. Meaningful only alongside :meth:`env`.
        """
        self._core.nest(separator)
        return self

    def allow_empty_env(self) -> DynamicConfig[M]:
        """Treats ``FOO=`` as set-to-empty rather than unset."""
        self._core.allow_empty_env()
        return self

    def strict_env(self) -> DynamicConfig[M]:
        """Refuses ambiguous environment spellings — ``off``, ``no``, ``nil``."""
        self._core.strict_env()
        return self

    def whole_document(self) -> DynamicConfig[M]:
        """Reads each document as this model's values, with no section header.

        The default is one file, several sections: every top-level key
        names one, and this configuration's key says which is yours —
        which is what lets a ``config.toml`` carry ``[db]`` and
        ``[server]`` for two models that know nothing about each other.

        Say this when the document is *only* this configuration::

            {"host": "0.0.0.0", "port": 8000}

            Server.config.whole_document().file("server.json").init()

        The key keeps its other jobs — ``APP_SERVER_PORT`` still reaches
        ``port``, the cache entry and the diagnostics are still named
        after it — and ``key=""`` is allowed for a configuration with
        nothing to call itself, whose environment layer is then just the
        prefix (``APP_PORT``). It applies to every document this
        configuration reads: files, discovered files, profile variants and
        a remote store's document alike.
        """
        self._core.whole_document()
        return self

    def env_file(self, path: str) -> DynamicConfig[M]:
        """A ``.env`` file read as the environment layer, below the real one."""
        self._core.env_file(str(path))
        return self

    def secrets_dir(self, path: str) -> DynamicConfig[M]:
        """A directory where each file is one key.

        How Docker and Kubernetes hand a container its credentials: the
        filename is the key, the contents are the value. One directory
        level, nesting spelled in the filename with the same separator
        :meth:`nest` sets, and one trailing newline trimmed — every tool
        that writes a secret to a file writes one and nobody means it as
        part of the password.

        Sits above the files and below ``.env`` and the environment: a
        mounted secret is a fact about *this* deployment, so it beats a
        document a store hands to every deployment alike, and loses to a
        variable exported for this one run.

        Values arrive as strings, deliberately — a credentials directory
        is the worst place to guess that ``12345`` was meant as a number.
        """
        self._core.secrets_dir(str(path))
        return self

    def profile_env(self, variable: str) -> DynamicConfig[M]:
        """The environment variable naming the active profile.

        Parameters:
            variable: the variable to read, e.g. ``APP_ENV``. Set to
                ``production``, every file gains a sibling layer —
                ``config.toml`` then ``config.production.toml`` — and a
                variant that does not exist is skipped. A profile has to
                be a plain word: one with a path separator in it is
                refused rather than followed.
        """
        self._core.profile_env(variable)
        return self

    def cache(self, path: str, mode: str = "redacted") -> DynamicConfig[M]:
        """A last-known-good cache, written after every clean load.

        Read when the sources will not load, so a restart during an
        outage starts from what worked rather than not at all.

        Parameters:
            path: where to write it. The format comes from the extension.
            mode: ``"redacted"`` — every secret path dropped, the usual
                choice — or ``"full"``, which writes the secrets too and
                is a file to protect accordingly, or ``"fingerprint"``,
                which stores no values at all and only reports whether
                the configuration changed. Anything else is a
                `ValueError` naming the three.

        A redacting mode has to know which paths are secret. A declared
        model says so; a :class:`~dynamic_config.Values` configuration
        says so with ``DynamicConfig(..., secrets=[...])``, and without
        it the cache is refused rather than written unredacted.
        """
        self._core.cache(str(path), mode)
        return self

    # ── The remote store ───────────────────────────────────────────────

    def remote(self, source: RemoteSource | AsyncRemoteSource) -> DynamicConfig[M]:
        """Reads this configuration's remote store from a Python object.

        ``source`` is a :class:`RemoteSource` — an object with ``fetch()``
        and ``describe()`` — so a store nobody will write a Rust client
        for is a class::

            class OurService(RemoteSource):
                def fetch(self):
                    return httpx.get(URL, timeout=5).text, Format.JSON

                def describe(self):
                    return "our service"

            config = DynamicConfig(Database, key="db").remote(OurService())

        Nothing is fetched here — call :meth:`refresh_remote` for that,
        which is the same explicit split the Rust crate makes: a load
        merges the document that was last fetched and touches no network.
        The remote layer sits above the files and below the environment.

        Unlike the source methods, this one is **not** refused after the
        first load: a store can be installed or swapped whenever, exactly
        as the Rust ``set_remote`` can. Swapping one drops whatever the
        previous store had fetched, so a new source never answers with the
        old one's values.

        ``describe()`` is asked once, here, because the engine reads it on
        the load path and a load must not re-enter Python.
        """
        if isinstance(source, AsyncRemoteSource):
            # An async store cannot be called from the engine's worker
            # thread, so what the engine gets is a courier: the coroutine
            # is awaited on the caller's loop, and this hands the result
            # across. Kept on `self` because `refresh_remote_async` is the
            # one that fills it.
            bridge = _AwaitedDocument(source)
            self._async_remote = bridge
            self._core.remote(bridge)
        else:
            self._async_remote = None
            self._core.remote(source)

        return self

    def refresh_remote(self) -> None:
        """Reads the store, and keeps what came back for the next load.

        Raises :class:`RemoteError` if the fetch failed — or
        :class:`AuthError` if what ``fetch()`` raised was one — with the
        exception the source raised attached as ``__cause__``. Its
        *message* is deliberately not repeated in this one: a store's
        exception routinely carries the URL it called.

        A failed refresh changes nothing: the document from the last
        successful fetch is still there, the installed model still serves,
        and a later refresh works. Nothing is poisoned by a store having a
        bad afternoon.

        Takes effect on the next :meth:`init` or :meth:`reload`.

        Raises `RuntimeError` if the installed store is an
        :class:`AsyncRemoteSource`: awaiting is what that store needs, and
        there is no loop here to await on.
        """
        # Checked here rather than left to the courier, which would have
        # the engine wrap it as a `RemoteError` whose message is withheld
        # — the right treatment for a store's exception, and the wrong one
        # for a caller who used the wrong method.
        if self._async_remote is not None:
            raise RuntimeError(
                f"{type(self._async_remote.source).__name__} is an "
                "AsyncRemoteSource: its fetch() is a coroutine, so call "
                "`await config.refresh_remote_async()` rather than "
                "`config.refresh_remote()`"
            )

        self._core.refresh_remote()

    async def refresh_remote_async(self) -> None:
        """:meth:`refresh_remote`, without blocking the event loop.

        For a :class:`RemoteSource` the fetch runs on a worker thread, so
        a ``fetch()`` written with a blocking client — which is most of
        them — does not stall the loop.

        For an :class:`AsyncRemoteSource` the coroutine is awaited right
        here, on the calling loop, and only the merge that follows goes to
        a thread. That ordering is the point: an async client belongs to
        the loop it was built on.
        """
        bridge = self._async_remote

        if bridge is not None:
            # Awaited first, and on this loop. A raising `fetch()` reaches
            # the caller as its own exception rather than as `RemoteError`,
            # because nothing has entered the engine yet — and a cancelled
            # task cancels the fetch, which is the thing a worker thread
            # could never offer.
            bridge.hand_over(await bridge.source.fetch())

        await asyncio.get_running_loop().run_in_executor(
            self._pool, self._core.refresh_remote
        )

    def clear_remote(self) -> None:
        """Drops the fetched document, so the next load has no remote layer.

        The source stays installed; this drops what was fetched, not where
        to fetch it from.
        """
        self._core.clear_remote()

    @property
    def remote_description(self) -> str | None:
        """What the installed store's ``describe()`` said, or ``None``."""
        return self._core.remote_description

    # ── Loading and installing ─────────────────────────────────────────

    def init(self) -> None:
        """Loads, validates and installs as this configuration's snapshot."""
        self._core.init()

    def init_and_current(self) -> M:
        """:meth:`init`, then :meth:`current` — the two calls that always pair.

        Starting up is the one moment a program wants both at once, and
        writing it as two statements means naming the configuration twice
        and reading the second line to find out the first one worked::

            db = DynamicConfig(Database, key="db").file("app.toml").init_and_current()

        The configuration object is still reachable when it is needed —
        a name you kept, or `Model.config` under the decorator — but a
        script, a test, or a module that only wants the values usually
        does not need one, and this is the shape that does not make it
        invent a name.

        Exactly `init()` followed by `current()`. A failure raises from
        the load, so there is no half-initialised state to hand back, and
        nothing installs twice.
        """
        self._core.init()

        return self.current()

    @property
    def _pool(self) -> Executor | None:
        """The executor the blocking half runs on; see :func:`set_executor`."""
        return self._executor if self._executor is not None else default_executor()

    async def init_async(self) -> None:
        """:meth:`init`, without blocking the event loop.

        Reading and parsing files is blocking work, so it happens on a
        worker thread with the GIL released; only the validate-and-swap
        step comes back. The same shape as the Rust ``init_async``, and
        for the same reason — a loop thread that stalls on disk I/O is a
        service that stops answering.
        """
        await asyncio.get_running_loop().run_in_executor(self._pool, self._core.init)

    async def init_and_current_async(self) -> M:
        """:meth:`init_and_current`, without blocking the event loop.

        The startup line for a service that has a loop already::

            config = DynamicConfig(Database, key="db").file("app.toml")
            db = await config.init_and_current_async()

        The load happens on a worker; the read that follows is an
        attribute lookup, which is why there is no second await.
        """
        await self.init_async()

        return self.current()

    def load(self) -> M:
        """Loads and validates, installing nothing. Returns the candidate."""
        return self._core.load()  # type: ignore[no-any-return]

    async def load_async(self) -> M:
        """:meth:`load`, without blocking the event loop."""
        return await asyncio.get_running_loop().run_in_executor(
            self._pool, self._core.load
        )

    def reload(self) -> None:
        """One reload: load, validate, install, rewrite the cache."""
        self._core.reload()

    async def reload_async(self) -> None:
        """:meth:`reload`, without blocking the event loop."""
        await asyncio.get_running_loop().run_in_executor(self._pool, self._core.reload)

    def current(self) -> M:
        """The installed model.

        One attribute lookup: the model is cached on this object and
        replaced by the engine when something installs. Raises
        :class:`NotInitialisedError` before the first successful load;
        :meth:`try_current` answers ``None`` instead.
        """
        model = self._cached

        if model is None:
            raise NotInitialisedError(
                f"{self._model.__name__} has not been loaded yet; call init() first"
            )

        return model

    def try_current(self) -> M | None:
        """The installed model, or ``None`` before the first load."""
        return self._cached

    def replace(self, model: M) -> None:
        """Installs a model built by the caller, firing the hooks.

        The engine is not told: `status()` and `snapshot().generation` go
        on describing the last real *load*, because this install never
        went through one. `current()` is the model handed over.
        """
        if not self._schema.is_instance(model):
            raise TypeError(
                f"expected a {self._model.__name__}, not {type(model).__name__}"
            )

        self._core.replace(model)

    # ── Watching, waking, hooks ────────────────────────────────────────

    def watch(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> Watch:
        """Reloads on file changes until the returned handle is stopped.

        Parameters:
            debounce: seconds to wait after a change before reloading. An
                editor's atomic save is several filesystem events, and
                this is what makes them one reload.
            poll_interval: seconds between polls, which chooses polling
                over the platform's notification backend — what network
                and overlay filesystems need, where notifications
                register successfully and then never fire. ``None`` uses
                the platform's own event API.

        Starting a watcher is short, but it is not free — see
        :meth:`watch_async` for what it costs and when that matters.
        """
        return Watch(self._core.watch(debounce, poll_interval))

    async def watch_async(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> Watch:
        """:meth:`watch`, without blocking the event loop.

        The watcher itself is a thread either way; what this moves off the
        loop is *starting* it. That means resolving the directories to
        observe, registering each with the platform's notification backend
        and spawning the carrier thread — syscalls, not I/O, but syscalls
        the calling thread waits for. Native registration measures a
        fraction of a millisecond and grows with the number of
        directories; ``poll_interval`` first takes a baseline scan of
        everything it watches, which is single-digit milliseconds over a
        large directory and worse over the network filesystems that are
        the reason to choose polling at all.

        A startup handler that runs once can afford either. Prefer this
        one anyway in a service: it is the same call with the wait moved
        to a worker, and the cost it avoids is largest exactly where you
        cannot measure it in advance.
        """
        core = self._core
        inner = await asyncio.get_running_loop().run_in_executor(
            self._pool, lambda: core.watch(debounce, poll_interval)
        )

        return Watch(inner)

    @contextmanager
    def watching(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> Iterator[Watch]:
        """:meth:`watch` as a block, stopped on the way out.

            with config.watching():
                serve()

        The handle returned by :meth:`watch` is already a context manager;
        this is the shape that never names it, and so cannot leak it by
        forgetting to::

            watch = config.watch()   # stopped only if nothing raises first
        """
        watch = self.watch(debounce, poll_interval)

        try:
            yield watch
        finally:
            watch.stop()

    @asynccontextmanager
    async def watching_async(
        self, debounce: float = 0.25, poll_interval: float | None = None
    ) -> AsyncIterator[Watch]:
        """:meth:`watching`, started off the loop."""
        watch = await self.watch_async(debounce, poll_interval)

        try:
            yield watch
        finally:
            watch.stop()

    @contextmanager
    def running(
        self,
        watch: bool = True,
        debounce: float = 0.25,
        poll_interval: float | None = None,
    ) -> Iterator[M]:
        """Load, watch, serve, stop — one block, yielding the first model.

            with config.running() as database:
                serve(database)

        ``database`` is the model as it was at startup; later installs go
        to ``current()`` and to the hooks, as they always do.
        """
        model = self.init_and_current()

        if not watch:
            yield model
            return

        with self.watching(debounce, poll_interval):
            yield model

    @asynccontextmanager
    async def running_async(
        self,
        watch: bool = True,
        debounce: float = 0.25,
        poll_interval: float | None = None,
    ) -> AsyncIterator[M]:
        """:meth:`running` for a service that starts on a loop.

        The shape a FastAPI ``lifespan`` wants::

            @asynccontextmanager
            async def lifespan(app):
                async with config.running_async() as database:
                    app.state.database = database
                    yield
        """
        model = await self.init_and_current_async()

        if not watch:
            yield model
            return

        async with self.watching_async(debounce, poll_interval):
            yield model

    def on_reload(
        self,
        hook: Callable[[M | None, M], None],
        *,
        dispatch: Dispatch | str | None = None,
        backpressure: Backpressure | str | None = None,
    ) -> HookGuard:
        """Runs ``hook(old, new)`` after every install.

        ``old`` is ``None`` for the first install and the previous model
        after that, so a hook can tell "starting up" from "changed"
        without keeping a flag of its own.

        A read inside a hook sees the *new* model: this configuration's
        own publish hook is registered first, deliberately, so
        ``current()`` agrees with the ``new`` argument rather than lagging
        it by one.

        By default the hook runs on whichever thread performed the reload
        — the watcher's, or the caller's for an explicit ``reload()`` — so
        keep it short: compare, then signal the subsystem that owns the
        resource. A raising hook is reported through Python's unraisable
        channel, and the hooks after it still run.

        Usable as a decorator; the guard it returns forwards calls to the
        function, so the name stays callable::

            @config.on_reload
            def resize(old, new):
                pool.resize(new.pool_size)

        Parameters:
            dispatch: where the hook runs — :attr:`Dispatch.INLINE`
                (the default) on the installing thread,
                :attr:`Dispatch.EXECUTOR` on the configuration executor,
                :attr:`Dispatch.ASYNCIO` as a task on the loop that
                registered it. A coroutine function defaults to
                ``asyncio`` and is refused by the other two, which have
                nothing to await it with.

                Anything but ``inline`` separates two latencies that are
                otherwise one: how long a reload takes, and how long the
                work it triggers takes. A hook that rebuilds a connection
                pool should not be able to delay the next install.
            backpressure: what happens when installs outrun the hook —
                :attr:`Backpressure.LATEST` (the default off the
                installing thread) keeps only the newest,
                :attr:`Backpressure.SERIAL` runs every one in order,
                :attr:`Backpressure.EVERY` starts each as it arrives, and
                :attr:`Backpressure.CANCEL_PREVIOUS` (``asyncio`` only)
                cancels the call still running.

                An ``inline`` hook is already one at a time, so ``every``
                is the only policy it can have.
        """
        # Always, even when neither argument was given: a coroutine
        # function registered with no `dispatch` would otherwise be called
        # inline, return a coroutine nobody awaits, and do nothing at all
        # except emit a warning at the next collection.
        wrapped = self._wrap_hook(hook, dispatch, backpressure)

        return HookGuard(self._core, self._core.on_reload(wrapped), hook)

    def _wrap_hook(
        self,
        hook: Callable[..., Any],
        dispatch: Dispatch | str | None,
        backpressure: Backpressure | str | None,
    ) -> Callable[[M | None, M], None]:
        """Turns a hook into the inline callable the engine calls.

        Whatever the policy, what the engine gets back is a *fast*
        synchronous function: it hands the work somewhere else and
        returns, so the install that triggered it is never waiting on the
        callback. That separation is the whole point of the parameter.
        """
        # A guard is a callable that forwards, so registering one again —
        # which is what `on_change_async` does, and what anyone chaining
        # two registrations does — must look through it to the function
        # underneath. Asking a guard whether it is a coroutine function
        # answers about the guard.
        target: Callable[..., Any] = hook

        if isinstance(hook, HookGuard) and hook.hook is not None:
            target = hook.hook

        coroutine = inspect.iscoroutinefunction(target)
        dispatch = (
            Dispatch(dispatch)
            if dispatch is not None
            else (Dispatch.ASYNCIO if coroutine else Dispatch.INLINE)
        )

        if coroutine and dispatch is not Dispatch.ASYNCIO:
            raise ValueError(
                f"{getattr(target, '__name__', target)!r} is a coroutine function, "
                f"which only dispatch={Dispatch.ASYNCIO.value!r} can run"
            )

        if not coroutine and dispatch is Dispatch.ASYNCIO:
            raise ValueError(
                f"dispatch={Dispatch.ASYNCIO.value!r} needs a coroutine "
                f"function; {getattr(target, '__name__', target)!r} is not one"
            )

        default = (
            Backpressure.EVERY if dispatch is Dispatch.INLINE else Backpressure.LATEST
        )
        policy = Backpressure(backpressure) if backpressure is not None else default

        if policy is Backpressure.CANCEL_PREVIOUS and dispatch is not Dispatch.ASYNCIO:
            raise ValueError(
                "cancel_previous cancels a task, and only "
                f"dispatch={Dispatch.ASYNCIO.value!r} has tasks to cancel"
            )

        if dispatch is Dispatch.INLINE:
            return hook

        if dispatch is Dispatch.ASYNCIO:
            return _asyncio_dispatcher(target, policy)

        # A callable rather than the pool itself: `configure_executor`
        # may not have been called yet when the hook is registered.
        return _executor_dispatcher(target, policy, lambda: self._pool)

    def on_reload_async(
        self,
        hook: Callable[[M | None, M], Awaitable[None]],
        *,
        backpressure: Backpressure | str = Backpressure.LATEST,
    ) -> HookGuard:
        """`on_reload` for a coroutine function.

        Sugar for ``on_reload(hook, dispatch=Dispatch.ASYNCIO)``, and the
        spelling an async program reaches for::

            @config.on_reload_async
            async def reconnect(previous, current):
                await pool.resize(current.pool.max_size)

        Register it from the loop that should run it: the watcher
        schedules the task with `call_soon_threadsafe` onto *that* loop and
        moves on, so a slow callback delays nothing but itself.
        """
        return self.on_reload(
            hook,  # type: ignore[arg-type]
            dispatch=Dispatch.ASYNCIO,
            backpressure=backpressure,
        )

    def on_change_async(
        self,
        *paths: str,
        backpressure: Backpressure | str = Backpressure.LATEST,
    ) -> Callable[[Callable[[M | None, M], Awaitable[None]]], HookGuard]:
        """:meth:`on_change` for a coroutine function.

        ::

            @config.on_change_async("redis.url")
            async def reconnect(previous, current):
                await redis.reconnect(current.redis.url)
        """

        def register(hook: Callable[[M | None, M], Awaitable[None]]) -> HookGuard:
            return self.on_change(*paths)(
                self._wrap_hook(hook, Dispatch.ASYNCIO, backpressure)
            )

        return register

    def on_change(
        self, *paths: str
    ) -> Callable[[Callable[[M | None, M], None]], HookGuard]:
        """A hook that runs only when one of ``paths`` actually moved.

        The filter almost every reload hook opens with, written once::

            @config.on_change("pool_size")
            def resize(old, new):
                pool.resize(new.pool_size)

        A reload installs a whole model whether or not the field you care
        about is in it — a neighbouring key changed, an operator re-saved
        the file, a watcher fired on a touch — and resizing a pool on
        every one of those is churn a service can feel. The comparison is
        :func:`changed_paths`, so it is paths-only and sees secrets
        without reporting them.

        Nested fields are dotted, as everywhere else, and a path naming a
        table fires when anything under it moves: ``on_change("pool")``
        covers ``pool.max_size``.

        The first install has no previous model to compare against, so it
        always counts as a change — a hook that *sets something up* runs
        at startup rather than waiting for the first edit.

        Returns the decorator; the guard it produces unregisters on
        ``close()`` or at the end of a ``with``.
        """
        if not paths:
            raise ValueError("on_change needs at least one path to watch")

        wanted = frozenset(paths)

        def register(hook: Callable[[M | None, M], None]) -> HookGuard:
            """Registers ``hook`` behind the path filter."""

            def filtered(previous: M | None, current: M) -> None:
                """Runs the hook when one of the paths moved, and not otherwise."""
                if previous is not None and not _touches(
                    wanted, changed_paths(previous, current)
                ):
                    return

                hook(previous, current)

            return HookGuard(self._core, self._core.on_reload(filtered), hook)

        return register

    @property
    def generation(self) -> int:
        """How many models have been installed. Zero before the first."""
        return int(self._core.generation)

    def changed(self, timeout: float | None = None) -> M | None:
        """Blocks until the next install, or until ``timeout`` elapses.

        For threads. On an event loop, await :meth:`changed_async` or
        iterate :meth:`changes`.

        Parameters:
            timeout: seconds to wait at most, or ``None`` to wait
                forever. Answers `None` when it elapses first.
        """
        result = self._core.wait_for_change(self._core.generation, timeout)

        return None if result is None else result[1]

    async def changed_async(self, timeout: float | None = None) -> M | None:
        """The next installed model, awaited once.

        :meth:`changes` is the iterator for a task that follows every
        reload; this is the single shot, for a task that wants to wait for
        one and move on.

        Nothing polls and nothing on the loop blocks: the wait is a future
        that a notifier thread resolves when the engine installs. That
        thread is shared by every awaiting task on this configuration and
        exists only while there is one — see :mod:`dynamic_config._notify`.
        """
        loop = asyncio.get_running_loop()

        # Read, register, read again — and the middle step is what makes
        # the outer two worth doing. An install that lands before the
        # registration is caught by the second read; one that lands after
        # it resolves the future. Nothing falls between them, which is the
        # same argument the engine's own `Changes` future makes.
        seen = int(self._core.generation)
        future = self._notifier(loop).wait(loop)

        if int(self._core.generation) > seen:
            future.cancel()

            return self.try_current()

        if timeout is None:
            wake = await future
        else:
            try:
                wake = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                return None

        if wake is None:  # released
            return None

        return wake[1]  # type: ignore[no-any-return]

    async def changes(self) -> AsyncIterator[M]:
        """Yields the installed model, once per wake, from here on.

        Latest wins rather than every-one-in-order: the wait answers with
        whatever is installed when it returns, so two installs inside one
        wait yield the second and not both. That is the right shape for
        configuration — a reader wants what is true now, not a queue of
        what briefly was — but it does mean this is not a log of installs,
        and a consumer counting them will count fewer than `generation`
        says. `on_reload` runs for every install, if that is what you
        need.

            async for db in config.changes():
                pool.resize(db.pool_size)

        Cancelling the loop that drives this is noticed immediately: the
        iterator is awaiting a future, not a timed wait somebody has to
        outlast.
        """
        loop = asyncio.get_running_loop()
        notifier = self._notifier(loop)
        seen = int(self._core.generation)

        while True:
            future = notifier.wait(loop)
            now = int(self._core.generation)

            if now > seen:
                # Installed while the consumer's body was running, or
                # between the two lines above. Latest wins, so what it
                # gets is what is installed — not a replay.
                future.cancel()
                seen = now
                model = self.try_current()

                if model is None:  # released
                    return

                yield model
                continue

            wake = await future

            if wake is None:  # released
                return

            generation, model = wake

            if generation <= seen:
                # The notifier's answer for an install this iterator has
                # already reported by reading the generation itself. Both
                # describe the same wake; one of them is enough.
                continue

            seen = generation

            yield model

    def events(self, failure_poll: float | None = None) -> AsyncIterator[ConfigEvent]:
        """Every install *and* every refusal, as typed events.

        :meth:`changes` is the model stream a service loop wants; this is
        the diagnostic one — what a health endpoint, a metric or a log
        line is built from::

            async for event in config.events():
                match event:
                    case Reloaded(generation=generation, changed=paths):
                        log.info("config %s: %s", generation, ", ".join(paths))
                    case ReloadFailed(kind=kind, path=path):
                        alert(f"configuration refused at {path}: {kind}")

        **No event carries a value.** Paths, kinds, counts and timestamps
        only — the same rule every other diagnostic here follows, and for
        the same reason: a value in an event is a secret in a log.

        A refusal wakes this stream **natively**: the engine's failure
        hook signals the same parked thread an install does, so
        :class:`ReloadFailed` arrives when the refusal happens, not at the
        next poll. Delivery is latest-wins, like :meth:`changes`: several
        refusals with nothing awake in between arrive as one event
        carrying the current ``consecutive`` count, and a refusal followed
        by an install arrives as both events, refusal first, because that
        is the order they occurred in.

        Parameters:
            failure_poll: **deprecated, ignored.** The interval refusals
                used to be polled at, when a refusal could not wake
                anything. It can now, so the stream no longer needs — or
                starts — a timer; passing a value changes nothing and
                warns once.
        """
        if failure_poll is not None:
            warnings.warn(
                "failure_poll is ignored: a refused reload wakes events() "
                "natively now, and nothing is polled",
                DeprecationWarning,
                stacklevel=2,
            )

        return self._events()

    async def _events(self) -> AsyncIterator[ConfigEvent]:
        loop = asyncio.get_running_loop()
        notifier = self._event_notifier(loop)
        seen = int(self._core.generation)
        seen_refusals = int(self._core.refusals)
        # The baseline for `changed`, and local to this stream: two
        # streams on one configuration each compare against what they
        # themselves last reported, rather than against each other.
        previous = self.try_current()

        while True:
            future = notifier.wait(loop)
            now = int(self._core.generation)
            now_refusals = int(self._core.refusals)

            if now > seen or now_refusals > seen_refusals:
                # Moved while the consumer's body was running, or between
                # the lines above. Latest wins: what it gets is what is
                # current, not a replay.
                future.cancel()
                model = self.try_current() if now > seen else None
                wake: Any = (now, now_refusals, model)
            else:
                wake = await future

            if wake is None:  # released
                return

            generation, refusals, model = wake

            if refusals > seen_refusals:
                seen_refusals = refusals
                status = self.status()

                yield _refusal(status, int(status.consecutive_failures))

            if generation <= seen:
                continue

            if model is None:  # released before the read landed
                return

            seen = generation
            status = self.status()
            before, previous = previous, model

            yield Reloaded(
                generation=int(status.generation),
                changed=tuple(change.path for change in changed_paths(before, model))
                if before is not None
                else (),
                reason=str(status.last_reason) if status.last_reason else "manual",
            )

    def set_default(self, path: str, value: Any) -> None:
        """A fallback the program computes and a file need not state.

        The bottom layer: consulted only when nothing else supplies the
        key. Takes effect on the next load.

        Parameters:
            path: the dotted path to set, e.g. ``pool.max_size``.
            value: any JSON-shaped value — `str`, `int`, `float`, `bool`,
                `None`, `list` or `dict` of those.
        """
        self._core.set_default(path, value)

    def set_defaults(self, values: Mapping[str, Any] | Any) -> None:
        """Every field of a mapping (or model) as defaults, at once.

        How a hand-written ``Config.default()`` becomes the bottom layer
        without naming each key.

        Parameters:
            values: a mapping, or a model instance to read the fields of.
        """
        self._core.set_defaults(values)

    def set_override(self, path: str, value: Any) -> None:
        """A value that outranks every source, the environment included.

        What makes a test authoritative, and what a ``--set key=value``
        flag reaches. Takes effect on the next load.

        Parameters:
            path: the dotted path to pin.
            value: as :meth:`set_default`.
        """
        self._core.set_override(path, value)
        self._overrides[path] = value

    def set_assignments(self, assignments: Sequence[str]) -> None:
        """``key=value`` strings, as a ``--set`` flag would supply them.

        Above the environment and below the overrides: a flag is more
        specific than a variable, and less specific than a value the
        program was told to pin.

        Parameters:
            assignments: the strings to parse. One with no ``=`` in it is
                an error naming it.
        """
        self._core.set_assignments(list(assignments))

    def clear_defaults(self) -> None:
        """Empties the defaults layer. Takes effect on the next load."""
        self._core.clear_defaults()

    def clear_overrides(self) -> None:
        """Empties the override layer — what a test does when it is done."""
        self._core.clear_overrides()
        self._overrides.clear()

    def clear_assignments(self) -> None:
        """Empties the flags layer."""
        self._core.clear_assignments()

    @contextmanager
    def overrides(self, **values: Any) -> Iterator[None]:
        """Pins values for a block, and puts the previous ones back after it.

        The shape a test wants, because the four-line version has a
        cleanup step that is easy to forget — and a forgotten
        `clear_overrides()` leaks into the next test through whatever
        configuration the module built::

            with config.overrides(pool_size=1, host="localhost"):
                assert config.current().pool_size == 1

        Reloaded on entry, so the values are live inside the block;
        reloaded again on the way out, so they are gone after it. The
        exit restores the override layer as it was *found* rather than
        emptying it, which is what lets a nested `with` compose: an inner
        block that pins one more field leaves the outer block's pins
        standing when it ends.

        A dotted path is spelled with `__`, the same nesting rule the
        environment layer uses — `pool__max_size=1` means
        `pool.max_size`. A field whose own name contains `__` cannot be
        written that way; use `set_override` for it.

        The restore runs on an exception too, which is the point: a
        failing assertion inside the block must not decide what the next
        test sees. With no arguments the block pins nothing and still
        restores, so a test that calls `set_override` itself can wrap
        that in one.
        """
        previous = dict(self._overrides)

        for name, value in values.items():
            self.set_override(name.replace("__", "."), value)

        try:
            self.reload()
            yield
        finally:
            self.clear_overrides()

            for path, value in previous.items():
                self.set_override(path, value)

            self.reload()

    def alias(self, old: str, new: str) -> None:
        """Keeps files written before a rename working.

        Fills a gap rather than overriding: the new path wins wherever it
        is set, and the old one answers only where it is not.

        Parameters:
            old: the path deployments still spell.
            new: the path it means now.
        """
        self._core.alias(old, new)

    def bind_env(self, path: str, variable: str) -> None:
        """Maps one field to one variable by name.

        For the variable a platform chose and a prefix cannot reach —
        ``PORT``, ``DATABASE_URL``. Sits just above the prefixed
        environment layer, because naming a variable is the more specific
        statement.

        Parameters:
            path: the dotted path to fill.
            variable: the environment variable to read it from.
        """
        self._core.bind_env(path, variable)

    # ── Diagnostics ────────────────────────────────────────────────────

    def source_of(self, path: str) -> Origin | None:
        """Where the value at ``path`` would come from on the next load.

        Re-reads the sources rather than reporting the installed
        snapshot, so it answers before the first load and after a failed
        one.

        Parameters:
            path: the dotted path to trace.
        """
        return Origin._of(self._core.source_of(path))

    def is_set(self, path: str) -> bool:
        """Whether anything supplies ``path``.

        Parameters:
            path: the dotted path to look for.
        """
        return bool(self._core.is_set(path))

    def explain(self, path: str) -> Explanation:
        """Every layer's answer for ``path``, not just the winner's.

        The one diagnostic that carries values, and the one that redacts:
        a path this configuration knows to be secret renders as ``***``.

        Parameters:
            path: the dotted path to explain.
        """
        raw = self._core.explain(path)

        return Explanation(
            path=raw["path"],
            rows=tuple(
                Contribution(
                    layer=row["layer"],
                    value=row["value"],
                    origin=Origin._of(
                        None
                        if row["origin_kind"] is None
                        else (row["origin_kind"], row["origin"])
                    ),
                )
                for row in raw["rows"]
            ),
            winner=raw["winner"],
            _rendered=raw["rendered"],
        )

    def check(self) -> Report:
        """What this configuration resolves to, and whether it would load."""
        raw = self._core.check()

        return Report(
            key=raw["key"],
            resolved=tuple(
                Resolved(
                    path=item["path"],
                    origin=Origin(kind=item["origin_kind"], detail=item["origin"]),
                )
                for item in raw["resolved"]
            ),
            unknown=tuple(
                UnknownKey(path=item["path"], suggestion=item["suggestion"])
                for item in raw["unknown"]
            ),
            failure=raw["failure"],
            unknown_checked=raw["unknown_checked"],
        )

    def snapshot(self) -> Snapshot:
        """The resolved section, without deserializing it into the model."""
        return Snapshot(self._core.snapshot())

    # ── Telemetry ──────────────────────────────────────────────────────

    def status(self) -> ConfigStatus:
        """What is true of this configuration right now.

        Which generation is live, how long ago it landed, why, and how
        the reloads since have gone — a handful of atomic loads and no
        I/O, so a ``/metrics`` handler may call it per scrape::

            status = config.status()
            if not status.is_healthy:
                log.warning("%d reloads have installed nothing",
                            status.consecutive_failures)

        The *when* fields are elapsed seconds rather than timestamps, and
        deliberately: the engine records them with a monotonic clock so
        that NTP stepping a wall clock backwards cannot make a fresh
        configuration look stale, and a monotonic instant has no epoch to
        convert from. :mod:`dynamic_config._telemetry` — the class
        docstrings on :class:`ConfigStatus` and :class:`Failure` — says
        what to do if a wall-clock time is what you actually need.

        Like the other diagnostics, asking fixes the sources: the numbers
        live in the engine, and this is what builds one.
        """
        return ConfigStatus._of(self._core.status())

    def remote_status(self) -> RemoteStatus:
        """How the fetches from this configuration's store have gone.

        The other half of the question :meth:`status` answers: *did the
        store answer*, against *did the document install*. A service
        watching only the second cannot tell a store that went away from
        a configuration nobody has changed.

        Always answers, even where no source was ever installed — that
        case reads as ``fetches == 0`` and ``reachable is None``, which
        is a source nobody has asked anything of rather than one that is
        down. Unlike :meth:`status` this touches no engine, so asking
        does not fix the sources.
        """
        return RemoteStatus._of(self._core.remote_status())

    def __repr__(self) -> str:
        """Model, key and generation — never the configuration itself."""
        return (
            f"<DynamicConfig {self._model.__name__} key={self.key!r} "
            f"generation={self.generation}>"
        )


class _Gate:
    """Serialises a hook's calls, under one of the backpressure policies.

    The engine calls a hook once per install and waits for it to return.
    Everything here exists so that "waits for it to return" stays true of
    a function that hands work away, and so that the work being handed
    away does not pile up without a rule for what to drop.
    """

    __slots__ = ("_lock", "_policy", "_queue", "_running", "_start")

    def __init__(self, policy: Backpressure, start: Callable[[Any, Any], None]) -> None:
        self._policy = policy
        self._start = start
        self._lock = threading.Lock()
        self._running = False
        self._queue: list[tuple[Any, Any]] = []

    def submit(self, previous: Any, current: Any) -> None:
        """Starts the hook now, queues the call, or drops it."""
        if self._policy is Backpressure.EVERY:
            self._start(previous, current)
            return

        with self._lock:
            if self._running and self._policy is Backpressure.LATEST:
                # One slot, overwritten: a pool being resized to a size
                # nobody wants any more is work done for nothing.
                self._queue = [(previous, current)]
                return

            if self._running and self._policy is Backpressure.SERIAL:
                self._queue.append((previous, current))
                return

            self._running = True

        self._start(previous, current)

    def finished(self) -> None:
        """Called by the runner when a call ends; starts whatever waited."""
        with self._lock:
            if not self._queue:
                self._running = False
                return

            previous, current = self._queue.pop(0)

        self._start(previous, current)


def _report(hook: Callable[..., Any], error: BaseException) -> None:
    """Reports a hook's exception the way a thread's is reported.

    The engine's rule for an inline hook is that a raising one is
    reported and the others still run; a hook that runs somewhere else
    keeps the rule, on the channel that fits where it ran. Nothing here
    reaches the install: a callback cannot fail a reload.
    """
    threading.excepthook(
        SimpleNamespace(  # type: ignore[arg-type]
            exc_type=type(error),
            exc_value=error,
            exc_traceback=error.__traceback__,
            thread=threading.current_thread(),
            hook=getattr(hook, "__name__", repr(hook)),
        )
    )


def _asyncio_dispatcher(
    hook: Callable[..., Any], policy: Backpressure
) -> Callable[[Any, Any], None]:
    """Schedules `hook` as a task on the loop that registered it.

    The loop is captured at registration rather than looked up at reload:
    a watcher thread has no running loop of its own, and asking for one
    there is how an async hook silently never runs.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            "an async hook has to be registered from the loop that should "
            "run it: there is no running loop here, and a watcher thread "
            "has none of its own to fall back on"
        ) from None

    running: list[asyncio.Task[Any]] = []

    async def run(previous: Any, current: Any) -> None:
        try:
            await hook(previous, current)
        except asyncio.CancelledError:
            # `cancel_previous`: whoever cancelled this has already
            # started its successor, so this one must not also start it.
            raise
        except Exception as error:
            loop.call_exception_handler(
                {
                    "message": (
                        f"dynamic-config reload hook "
                        f"{getattr(hook, '__name__', hook)!r} raised"
                    ),
                    "exception": error,
                }
            )

        gate.finished()

    def start(previous: Any, current: Any) -> None:
        task = loop.create_task(run(previous, current))
        running.clear()
        running.append(task)

    gate = _Gate(policy, start)

    def dispatch(previous: Any, current: Any) -> None:
        """What the engine calls, on the installing thread. It returns at once."""

        def on_loop() -> None:
            if policy is Backpressure.CANCEL_PREVIOUS:
                for task in running:
                    if not task.done():
                        task.cancel()

                start(previous, current)
                return

            gate.submit(previous, current)

        # The hop that keeps a reload cheap: the watcher thread only
        # queues a callback, and the loop does everything after. A closed
        # loop raises, and a callback nobody can run is nothing to report.
        with contextlib.suppress(RuntimeError):  # pragma: no cover
            loop.call_soon_threadsafe(on_loop)

    return dispatch


def _executor_dispatcher(
    hook: Callable[..., Any], policy: Backpressure, pool: Callable[[], Any]
) -> Callable[[Any, Any], None]:
    """Runs `hook` on the configuration executor, off the installing thread.

    ``pool`` is read at every dispatch rather than captured, so a hook
    registered before :func:`~dynamic_config.configure_executor` still
    lands in the pool the program eventually chose.
    """

    def run(previous: Any, current: Any) -> None:
        try:
            hook(previous, current)
        except Exception as error:
            _report(hook, error)

        gate.finished()

    def start(previous: Any, current: Any) -> None:
        executor = pool()

        if executor is None:
            # No configured executor: a thread of its own rather than the
            # installing one, which is the whole point of this dispatch.
            threading.Thread(
                target=run,
                args=(previous, current),
                name="dynamic-config-hook",
                daemon=True,
            ).start()
        else:
            executor.submit(run, previous, current)

    gate = _Gate(policy, start)

    return gate.submit
