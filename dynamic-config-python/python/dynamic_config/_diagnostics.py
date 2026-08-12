"""The answers the diagnostics give, as data rather than as text.

`source_of`, `explain`, `check` and `snapshot` each hand back one of
these. They are frozen dataclasses so a caller can pattern-match, log or
serialise them, and their `repr` shows *shape* rather than values —
`explain` is the one object here that carries values, and it redacts
secrets before it renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _core

# ── Diagnostics, as data ───────────────────────────────────────────────


@dataclass(frozen=True)
class Origin:
    """Where a value came from."""

    kind: str
    """One of ``file``, ``env``, ``inline``, ``remote``, ``runtime`` or
    ``unknown``."""

    detail: str | None = None
    """The path, the variable name, the store — whatever the kind names."""

    def __str__(self) -> str:
        """The rendering the Rust crate uses: *in /etc/app.toml*."""
        if self.kind == "file":
            return f"in {self.detail}"
        if self.kind == "env":
            return f"from {self.detail}"
        if self.kind == "remote":
            return f"from the remote store {self.detail}"
        if self.kind == "runtime":
            return f"set at runtime ({self.detail})"
        if self.kind == "inline":
            return "in an inline source"

        return "from an unknown source"

    @staticmethod
    def _of(raw: tuple[str, str | None] | None) -> Origin | None:
        """Builds one from the pair the engine hands over, or ``None``."""
        return None if raw is None else Origin(kind=raw[0], detail=raw[1])


@dataclass(frozen=True)
class Contribution:
    """What one layer had to say about a path."""

    layer: str
    value: str | None
    origin: Origin | None


@dataclass(frozen=True)
class Explanation:
    """Every layer's answer for one path, and which one wins.

    The one diagnostic that carries values — and the one that redacts:
    a path under a ``SecretStr`` field reads ``***``, because the secret
    list was derived from the model itself.
    """

    path: str
    rows: tuple[Contribution, ...]
    winner: str | None
    _rendered: str = field(repr=False, default="")

    def __str__(self) -> str:
        """The table, values included — this is the one place they belong."""
        return self._rendered

    def __repr__(self) -> str:
        """Shape, never values.

        ``str()`` is where this object's values are meant to appear; a
        ``repr`` lands in a log line by accident, which is precisely the
        difference.
        """
        return f"<Explanation path={self.path!r} layers={len(self.rows)}>"


@dataclass(frozen=True)
class Change:
    """One path that moved between two configurations.

    Paths, never values — the audit half of a reload, holding the same
    line every other diagnostic here holds.
    """

    path: str
    kind: str
    """``added``, ``removed`` or ``changed``."""

    def __str__(self) -> str:
        """``pool.max_size changed`` — the path, and what happened to it."""
        return f"{self.path} {self.kind}"


@dataclass(frozen=True)
class Resolved:
    """One resolved leaf, and where it came from."""

    path: str
    origin: Origin


@dataclass(frozen=True)
class UnknownKey:
    """A key the sources supply and the model does not declare."""

    path: str
    suggestion: str | None


@dataclass(frozen=True)
class Report:
    """What a configuration resolves to, and whether it would load."""

    key: str
    resolved: tuple[Resolved, ...]
    unknown: tuple[UnknownKey, ...]
    failure: str | None

    @property
    def is_clean(self) -> bool:
        """Nothing unknown, nothing failing."""
        return not self.unknown and self.failure is None

    def __repr__(self) -> str:
        """Counts rather than contents: a report lands in logs."""
        return (
            f"<Report key={self.key!r} resolved={len(self.resolved)} "
            f"unknown={len(self.unknown)} clean={self.is_clean}>"
        )


class Snapshot:
    """The resolved section, values and provenance, without a model."""

    __slots__ = ("_inner",)

    def __init__(self, inner: _core.Snapshot) -> None:
        """Wraps the engine's snapshot. Obtained from ``config.snapshot()``."""
        self._inner = inner

    def to_dict(self) -> dict[str, Any]:
        """The resolved values as plain Python data."""
        return self._inner.to_dict()

    def source_of(self, path: str) -> Origin | None:
        """Where the value at ``path`` came from, in this snapshot."""
        return Origin._of(self._inner.source_of(path))

    def contains(self, path: str) -> bool:
        """Whether this snapshot has a value at ``path``."""
        return self._inner.contains(path)

    def leaf_paths(self) -> list[str]:
        """Every dotted path that holds a value, tables walked through."""
        return self._inner.leaf_paths()

    def top_level_keys(self) -> list[str]:
        """The section's own keys, one level deep."""
        return self._inner.top_level_keys()

    def is_empty(self) -> bool:
        """Whether anything resolved at all."""
        return self._inner.is_empty()

    def diff(self, other: Snapshot) -> list[Change]:
        """Which paths moved between two snapshots — paths, never values."""
        return [
            Change(path=path, kind=kind)
            for path, kind in self._inner.diff(other._inner)
        ]

    def __repr__(self) -> str:
        """Shape only — a snapshot holds resolved values, secrets included."""
        return repr(self._inner)


def changed_paths(
    previous: Any,
    current: Any,
) -> list[Change]:
    """Which paths differ between two configurations.

    The audit half of a reload — what an ``on_reload`` hook logs when it
    wants to say *what* moved without saying what it moved to::

        config.on_reload(
            lambda old, new: log.info("config changed: %s", changed_paths(old, new))
        )

    Paths only, never values, whatever the models hold.
    """
    return [
        Change(path=path, kind=kind)
        for path, kind in _core.changed_paths(previous, current)
    ]
