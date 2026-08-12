"""The one exception this side adds to the engine's hierarchy.

Everything else — `IoError` through `BackendError` — is raised by the
compiled half, which mirrors the crate's `ErrorKind` one for one.
"""

from __future__ import annotations

from ._core import DynamicConfigError


class NotInitialisedError(DynamicConfigError):
    """Raised by ``current()`` before anything has been installed.

    ``try_current()`` answers ``None`` instead, for code that would
    rather ask than catch.
    """
