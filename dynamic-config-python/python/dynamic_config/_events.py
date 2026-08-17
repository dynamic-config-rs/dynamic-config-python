"""What `events()` yields.

Frozen dataclasses rather than tuples, so `match` reads as prose and a
field cannot be assigned into by accident. Every field is a path, a kind,
a count or a timestamp — **never a value out of the document**. A stream
somebody pipes into a log is exactly where a secret would end up, and the
rule is the same one `explain()` and `check()` already follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time

__all__ = ["ConfigEvent", "ReloadFailed", "Reloaded"]


@dataclass(frozen=True)
class ConfigEvent:
    """The base of the stream. `at` is a Unix timestamp, in seconds."""

    generation: int
    at: float = field(default_factory=time)


@dataclass(frozen=True)
class Reloaded(ConfigEvent):
    """A document installed."""

    changed: tuple[str, ...] = ()
    """The dotted paths whose values moved, from `changed_paths`. Empty on
    the first install, where there is nothing to compare against."""

    reason: str = "manual"
    """`initial`, `file changed`, `remote changed`, `manual`, `recovered` —
    whatever the engine recorded, as a word rather than a path."""


@dataclass(frozen=True)
class ReloadFailed(ConfigEvent):
    """A document was refused, and the previous one is still serving."""

    kind: str = "unknown"
    """The error kind: `io`, `parse`, `missing`, `type`, `env`, `invalid`,
    `remote`, `auth`, `decrypt`, `backend`."""

    path: str = ""
    """The dotted key the failure is about, or `""` when it is the load's
    rather than a field's."""

    consecutive: int = 0
    """How many refusals in a row — the number an alert is written
    against, because one refused reload is a typo and nine is an
    outage."""
