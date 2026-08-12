"""The configuration object: a model, where its values live, and its lifecycle.

Sources are chosen fluently and take effect at the first load; everything
after that — install, reload, watch, hooks, diagnostics — mirrors the Rust
crate one call at a time. The read path deliberately never crosses back
into Rust: each installed model is published onto this object by a hook,
so `current()` is an attribute lookup.
"""

from __future__ import annotations

import asyncio
import warnings
import weakref
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from concurrent.futures import Executor
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
from ._errors import NotInitialisedError
from ._executor import default_executor
from ._lifetime import _LIVE_CONFIGS, HookGuard, Watch
from ._schema import schema_for
from ._settings import (
    _SETTINGS_SOURCING,
    _as_paths,
    _declared_sourcing,
    _is_settings,
    _leaf_paths,
)

M = TypeVar("M")


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
    """One configuration: a Pydantic model, and where its values live.

    Sources are chosen fluently and take effect at the first load; the
    lifecycle after that mirrors the Rust crate one for one.
    """

    __slots__ = ("__weakref__", "_cached", "_core", "_executor", "_model", "_schema")

    def __init__(
        self,
        model: type[M],
        key: str,
        *,
        executor: Executor | None = None,
    ) -> None:
        """Builds a configuration for ``model`` under the section ``key``.

        Nothing is read here: sources are chosen with the fluent methods
        and take effect at the first load. ``executor`` chooses which
        thread pool pays for the blocking half of the ``_async`` calls;
        ``None`` follows :func:`set_executor`.

        The secret list and the field names are derived from the model
        itself — nobody keeps a second list of which fields are sensitive
        in step with the first.
        """
        # Whatever kind of schema this is — a Pydantic model, a plain
        # dataclass — the adapter answers the same three questions and
        # nothing below here knows the difference. A class this package
        # cannot read is refused right at the door.
        schema = schema_for(model)

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
        # `None` means "whatever `set_executor` says", which in turn
        # means "the loop's own" unless somebody said otherwise.
        self._executor = executor
        self._schema = schema
        self._core = _core.Config(
            schema.validate,
            key,
            schema.secret_paths(),
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

        _LIVE_CONFIGS.add(self)

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

        What cannot be translated is refused rather than dropped:
        ``secrets_dir`` (the engine has no directory-of-values source),
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
            for name in ("secrets_dir", "cli_parse_args")
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
        """Adds a configuration file. Merged in call order; later wins."""
        self._core.file(str(path))
        return self

    def discover(self, name: str, paths: Iterable[str]) -> DynamicConfig[M]:
        """Looks for ``{name}.{ext}`` in each directory, below listed files."""
        self._core.discover(name, [str(path) for path in paths])
        return self

    def env(self, prefix: str) -> DynamicConfig[M]:
        """The environment layer: ``prefix`` plus the section key."""
        self._core.env(prefix)
        return self

    def nest(self, separator: str) -> DynamicConfig[M]:
        """The nesting separator inside variable names; ``__`` unless said."""
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

    def env_file(self, path: str) -> DynamicConfig[M]:
        """A ``.env`` file read as the environment layer, below the real one."""
        self._core.env_file(str(path))
        return self

    def profile_env(self, variable: str) -> DynamicConfig[M]:
        """The environment variable naming the active profile."""
        self._core.profile_env(variable)
        return self

    def cache(self, path: str, mode: str = "redacted") -> DynamicConfig[M]:
        """A last-known-good cache, written after every clean load.

        ``mode`` is ``redacted`` (the default — secrets stay out),
        ``full`` or ``fingerprint``.
        """
        self._core.cache(str(path), mode)
        return self

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
        """Installs a model built by the caller, firing the hooks."""
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

        ``poll_interval`` chooses polling over the platform's notification
        backend — what network and overlay filesystems need, where
        notifications register successfully and then never fire.

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

    def on_reload(self, hook: Callable[[M | None, M], None]) -> HookGuard:
        """Runs ``hook(old, new)`` after every install.

        ``old`` is ``None`` for the first install and the previous model
        after that, so a hook can tell "starting up" from "changed"
        without keeping a flag of its own.

        A read inside a hook sees the *new* model: this configuration's
        own publish hook is registered first, deliberately, so
        ``current()`` agrees with the ``new`` argument rather than lagging
        it by one.

        The hook runs on whichever thread performed the reload — the
        watcher's, or the caller's for an explicit ``reload()`` — so keep
        it short: compare, then signal the subsystem that owns the
        resource. A raising hook is reported through Python's unraisable
        channel, and the hooks after it still run.

        Usable as a decorator; the guard it returns forwards calls to the
        function, so the name stays callable::

            @config.on_reload
            def resize(old, new):
                pool.resize(new.pool_size)
        """
        return HookGuard(self._core, self._core.on_reload(hook), hook)

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
        """
        result = self._core.wait_for_change(self._core.generation, timeout)

        return None if result is None else result[1]

    async def changed_async(self, timeout: float | None = None) -> M | None:
        """The next installed model, awaited once.

        :meth:`changes` is the iterator for a task that follows every
        reload; this is the single shot, for a task that wants to wait for
        one and move on. The wait itself happens off the loop with the GIL
        released, so nothing else on the loop is delayed by it.
        """
        seen = self._core.generation
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        while True:
            # Bounded slices rather than one long block: cancelling this
            # task should be noticed in a quarter second, not at the next
            # reload — which may never come.
            slice_timeout = 0.25

            if deadline is not None:
                remaining = deadline - loop.time()

                if remaining <= 0:
                    return None

                slice_timeout = min(slice_timeout, remaining)

            result = await loop.run_in_executor(
                None, self._core.wait_for_change, seen, slice_timeout
            )

            if result is not None:
                return result[1]  # type: ignore[no-any-return]

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

        Runtime-agnostic in the same spirit as the Rust ``changes()``: the
        wait happens on a worker thread with the GIL released, so any
        event loop drives it.

            async for db in config.changes():
                pool.resize(db.pool_size)
        """
        seen = self._core.generation
        loop = asyncio.get_running_loop()

        while True:
            # A bounded wait, so cancelling the iterator is noticed within
            # a quarter second rather than at the next reload — which may
            # never come.
            result = await loop.run_in_executor(
                None, self._core.wait_for_change, seen, 0.25
            )

            if result is None:
                continue

            seen, model = result
            yield model

    # ── Runtime layers ─────────────────────────────────────────────────

    def set_default(self, path: str, value: Any) -> None:
        """A fallback the program computes and a file need not state."""
        self._core.set_default(path, value)

    def set_defaults(self, values: Mapping[str, Any] | Any) -> None:
        """Every field of a mapping (or model) as defaults, at once."""
        self._core.set_defaults(values)

    def set_override(self, path: str, value: Any) -> None:
        """A value that outranks every source — what makes a test authoritative."""
        self._core.set_override(path, value)

    def set_assignments(self, assignments: Sequence[str]) -> None:
        """``key=value`` strings, as a ``--set`` flag would supply them."""
        self._core.set_assignments(list(assignments))

    def clear_defaults(self) -> None:
        """Empties the defaults layer. Takes effect on the next load."""
        self._core.clear_defaults()

    def clear_overrides(self) -> None:
        """Empties the override layer — what a test does when it is done."""
        self._core.clear_overrides()

    def clear_assignments(self) -> None:
        """Empties the flags layer."""
        self._core.clear_assignments()

    def alias(self, old: str, new: str) -> None:
        """Keeps files written before a rename working."""
        self._core.alias(old, new)

    def bind_env(self, path: str, variable: str) -> None:
        """Maps one field to one variable by name — ``PORT``, ``DATABASE_URL``."""
        self._core.bind_env(path, variable)

    # ── Diagnostics ────────────────────────────────────────────────────

    def source_of(self, path: str) -> Origin | None:
        """Where the value at ``path`` would come from on the next load."""
        return Origin._of(self._core.source_of(path))

    def is_set(self, path: str) -> bool:
        """Whether anything supplies ``path``."""
        return bool(self._core.is_set(path))

    def explain(self, path: str) -> Explanation:
        """Every layer's answer for ``path``, not just the winner's."""
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
        )

    def snapshot(self) -> Snapshot:
        """The resolved section, without deserializing it into the model."""
        return Snapshot(self._core.snapshot())

    def __repr__(self) -> str:
        """Model, key and generation — never the configuration itself."""
        return (
            f"<DynamicConfig {self._model.__name__} key={self.key!r} "
            f"generation={self.generation}>"
        )
