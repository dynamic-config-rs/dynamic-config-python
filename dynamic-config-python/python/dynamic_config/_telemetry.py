"""What is true of a configuration right now, and how to publish it.

Two halves, and they answer adjacent questions. :class:`ConfigStatus` is
*did the document install*; :class:`RemoteStatus` is *did the store
answer*. A service that watches only the first cannot tell a store that
went away from a configuration nobody has changed.

    status = config.status()
    if not status.is_healthy:
        log.warning("config has failed %d reloads", status.consecutive_failures)

:class:`Exposition` renders either as Prometheus text, and that is all it
does — this package chooses no metrics ecosystem for the service
importing it, exactly as the Rust crate's `telemetry` feature chooses
none. Mount the string on whatever `/metrics` route you already have.

## Why there are no timestamps here

The engine records *when* with `std::time::Instant`, deliberately: those
numbers are read as **how long ago**, and a wall clock stepping backwards
under NTP would make a freshly loaded configuration look stale. An
`Instant` has no epoch to convert from — not Unix's, and not
`time.monotonic()`'s, which is a different clock read from a different
origin even inside this one process.

So what crosses is the elapsed seconds, as a float, measured when the
status was taken: :attr:`ConfigStatus.stale_for`,
:attr:`RemoteStatus.stale_for`, and :attr:`Failure.seconds_ago`. There is
deliberately no `loaded_at` and no `datetime`: building one by
subtracting the elapsed time from `time.time()` would hand back a number
that claims a precision the engine refused to claim, and would be wrong
in exactly the case the monotonic clock was chosen for. If a service
needs a wall-clock timestamp, it should take one itself in an
`on_reload` hook — that is its clock, and its decision.

## What is not in here

No configured value, at all: a status is counts, durations and two fixed
enums, so there is nowhere for one to hide. No store address — the only
string a source can produce for itself is `describe()`, which is a URL
and routinely carries `user:password@host`. The dotted key path a
:class:`Failure` carries stops at the status object: the exposition
renders the failure's *category* and never its path, because a path is
unbounded label cardinality as well as a detail nobody asked to publish.
Every label in the output is a string the caller passed in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import _core

if TYPE_CHECKING:  # pragma: no cover - import cycle, and only mypy needs it
    from ._config import DynamicConfig

# ── A status, as data ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Failure:
    """A reload that installed nothing, or a fetch that returned nothing.

    Kept after a later success, because it is history — the health is
    ``consecutive_failures`` on the status that holds this.
    """

    kind: str
    """The error's category: ``io``, ``parse``, ``missing``, ``type``,
    ``env``, ``invalid``, ``remote``, ``auth``, ``decrypt`` or
    ``backend``. The same names the exception classes carry."""

    path: str
    """The dotted key path it was reported at; empty when the failure
    belongs to the load as a whole rather than to one key.

    A path, never a value — the line every diagnostic in this package
    holds. It stays out of the exposition all the same."""

    seconds_ago: float
    """How long before the status was taken this was recorded."""

    @staticmethod
    def _of(raw: Mapping[str, Any] | None) -> Failure | None:
        """Builds one from the dict the engine hands over, or ``None``."""
        if raw is None:
            return None

        return Failure(
            kind=str(raw["kind"]),
            path=str(raw["path"]),
            seconds_ago=float(raw["seconds_ago"]),
        )


@dataclass(frozen=True)
class ConfigStatus:
    """What is true of a configuration right now, for an operator asking.

    A snapshot of a handful of atomic loads: nothing is re-read, no
    source is touched, and nothing here can block — which is what makes
    it cheap enough to take on every scrape.
    """

    generation: int
    """Models installed since the process started; zero before the first."""

    stale_for: float | None
    """Seconds since the serving model was installed, or ``None`` before
    the first install.

    The one number an alert is usually written against: *this service's
    configuration has been stale for an hour* is the page that matters.
    ``None`` rather than zero before anything has loaded, because zero
    would read as "installed just now", which is the opposite."""

    last_reason: str | None
    """Why the serving model was installed — ``initial``, ``manual``,
    ``file_changed``, ``remote``, ``replaced`` — or ``None`` before the
    first install."""

    last_failure: Failure | None
    """The most recent reload that installed nothing, if there has been
    one."""

    consecutive_failures: int
    """Reloads that installed nothing since one did. **Zero is healthy.**"""

    is_healthy: bool
    """Whether the last attempt to load installed something.

    ``consecutive_failures == 0``, decided by the engine rather than
    recomputed here: a second spelling of a rule is how two surfaces come
    to disagree about it. True before the first load too — nothing has
    failed yet."""

    @staticmethod
    def _of(raw: Mapping[str, Any]) -> ConfigStatus:
        """Builds one from the dict the engine hands over."""
        return ConfigStatus(
            generation=int(raw["generation"]),
            stale_for=None if raw["stale_for"] is None else float(raw["stale_for"]),
            last_reason=None if raw["last_reason"] is None else str(raw["last_reason"]),
            last_failure=Failure._of(raw["last_failure"]),
            consecutive_failures=int(raw["consecutive_failures"]),
            is_healthy=bool(raw["is_healthy"]),
        )


@dataclass(frozen=True)
class RemoteStatus:
    """How the fetches from a configuration's remote store have gone.

    The shape of :class:`ConfigStatus` on purpose, because the two answer
    adjacent questions — *did the store answer* against *did the document
    install* — and a second vocabulary is how two surfaces come to
    disagree.
    """

    fetches: int
    """Documents the store has handed over since the process started,
    whether pulled by ``refresh_remote()`` or pushed by a watch."""

    stale_for: float | None
    """Seconds since the store last handed one over; ``None`` before the
    first."""

    last_fetch_duration: float | None
    """How long the last *pulled* fetch took, in seconds.

    ``None`` before the first pull, and ``None`` again after a document
    arrives by push: reporting the previous pull's duration beside a
    push's timestamp would be a number that is not about the fetch it
    appears to describe."""

    last_failure: Failure | None
    """The most recent fetch that returned nothing, if there has been one."""

    consecutive_failures: int
    """Fetches that returned nothing since one returned a document.
    **Zero is healthy.**"""

    reachable: bool | None
    """Whether the store answered the last time it was asked.

    Three states, and the third is the point: ``None`` before anything
    has been asked of the store at all. A source that has been installed
    and never fetched is not *down*, and reporting it as down is how a
    scrape at startup pages somebody."""

    @staticmethod
    def _of(raw: Mapping[str, Any]) -> RemoteStatus:
        """Builds one from the dict the engine hands over."""
        return RemoteStatus(
            fetches=int(raw["fetches"]),
            stale_for=None if raw["stale_for"] is None else float(raw["stale_for"]),
            last_fetch_duration=(
                None
                if raw["last_fetch_duration"] is None
                else float(raw["last_fetch_duration"])
            ),
            last_failure=Failure._of(raw["last_failure"]),
            consecutive_failures=int(raw["consecutive_failures"]),
            reachable=None if raw["reachable"] is None else bool(raw["reachable"]),
        )


# ── The exposition ─────────────────────────────────────────────────────


class Exposition:
    """One or more configurations' status, as a Prometheus text body.

    Built per scrape and thrown away — every sample comes from a status,
    which is atomic loads and no I/O, so there is nothing here worth
    keeping between scrapes and nothing to go stale::

        @app.get("/metrics")
        def metrics() -> Response:
            body = Exposition().add("db", config).add_remote("db", config).render()
            return Response(body, media_type="text/plain; version=0.0.4")

    The families, their names and the rule that a fact which does not
    exist yet is *absent* rather than zero are all the Rust crate's; this
    is a door on it. What it emits, per configuration added:

    ==================================================== ======= ===========
    Name                                                 Type    Extra label
    ==================================================== ======= ===========
    ``dynamic_config_installs_total``                    counter
    ``dynamic_config_last_success_seconds``              gauge
    ``dynamic_config_consecutive_failures``              gauge
    ``dynamic_config_last_failure_seconds``              gauge
    ``dynamic_config_last_reload_info``                  gauge   ``reason``
    ``dynamic_config_last_failure_info``                 gauge   ``kind``
    ==================================================== ======= ===========

    and per remote source added:

    ==================================================== ======= ===========
    Name                                                 Type    Extra label
    ==================================================== ======= ===========
    ``dynamic_config_remote_up``                         gauge
    ``dynamic_config_remote_fetches_total``              counter
    ``dynamic_config_remote_last_fetch_seconds``         gauge
    ``dynamic_config_remote_last_fetch_duration_seconds`` gauge
    ``dynamic_config_remote_consecutive_failures``       gauge
    ``dynamic_config_remote_last_failure_info``          gauge   ``kind``
    ==================================================== ======= ===========

    **These names are API.** They end up in dashboards and alert rules,
    so a rename is a breaking change.

    Cardinality is bounded and the bound is the caller's: six series per
    configuration per scrape, six more per remote source, and the label
    sets come from fixed enums — five reload reasons and ten error
    kinds. No key path, file name or configured value is ever a label,
    and there is no method here that could make one: the labels are the
    strings you pass, and everything derived from a status is a count, a
    duration or a fixed enum's name.
    """

    __slots__ = ("_inner",)

    def __init__(self) -> None:
        """An empty exposition. Add configurations to it, then render."""
        self._inner = _core.Exposition()

    def add(self, name: str, config: DynamicConfig[Any]) -> Exposition:
        """Adds ``config``'s status, labelled ``config="{name}"``.

        The name is yours: a section key, a service name, whatever
        distinguishes this configuration from the others in the process.
        **Not** a path or anything derived from the document.

        Returns this exposition, so the calls chain.
        """
        return self.add_with({"config": name}, config)

    def add_with(
        self, labels: Mapping[str, str], config: DynamicConfig[Any]
    ) -> Exposition:
        """:meth:`add`, with labels of your choosing.

        For a process whose configurations need two dimensions rather
        than one — an application and a profile, say. Label names are
        sanitised to Prometheus's ``[a-zA-Z_][a-zA-Z0-9_]*`` and values
        are escaped, so no caller can break out of the exposition; the
        *cardinality* of what you pass stays your decision.
        """
        self._inner.add(list(labels.items()), config._core)
        return self

    def add_remote(self, name: str, config: DynamicConfig[Any]) -> Exposition:
        """Adds ``config``'s remote source, labelled ``config="{name}"``.

        Usually the same name :meth:`add` was given, so the two halves
        join in a query: ``dynamic_config_remote_up`` and
        ``dynamic_config_last_success_seconds`` for one ``config`` are
        *the store answered* against *the document installed*, which is
        the pair an operator is comparing.

        **Never the store's URL**, and there is no overload that takes
        one: a store URL routinely embeds ``user:password@host``, so the
        name a series carries is yours — the same rule, and the same
        reason, as for a key path.
        """
        return self.add_remote_with({"config": name}, config)

    def add_remote_with(
        self, labels: Mapping[str, str], config: DynamicConfig[Any]
    ) -> Exposition:
        """:meth:`add_remote`, with labels of your choosing."""
        self._inner.add_remote(list(labels.items()), config._core)
        return self

    def render(self) -> str:
        """The exposition, as a Prometheus text body.

        The durations are measured here rather than when a configuration
        was added, so the seconds in the body are as fresh as the
        response is.
        """
        return self._inner.render()

    def __repr__(self) -> str:
        """No labels and no numbers — a ``repr`` lands in a log line."""
        return "<Exposition>"
