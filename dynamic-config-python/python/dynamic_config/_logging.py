"""The engine's diagnostics, through `logging` — and the knobs on that.

From the first import of this package, the compiled engine's own lines —
one per reload, a warning per failed one, the last-known-good recovery
notice — arrive as ordinary records on ``logging.getLogger
("dynamic_config.engine")`` instead of being written straight to file
descriptor 2. Handlers, formatters, filters, ``caplog``: everything that
works on a normal logger works on these.

Nothing here needs to be called. This module exists for the deployments
that want something else:

    import dynamic_config

    dynamic_config.configure_logging(level=logging.WARNING)  # quieter
    dynamic_config.configure_logging(raw_stderr=True)        # 0.6 behaviour
"""

from __future__ import annotations

import atexit

from . import _core

__all__ = ["configure_logging"]


def configure_logging(
    *,
    level: int | None = None,
    raw_stderr: bool = False,
) -> None:
    """Adjusts how the engine's own diagnostics are delivered.

    Parameters:
        level: how loud the *engine* is, in `logging`'s units — at or
            below ``logging.INFO`` everything is forwarded (the default),
            above it only warnings, negative silences the engine
            entirely. This gates emission on the Rust side; the logger's
            own level and handlers still apply on top.
        raw_stderr: ``True`` restores the pre-0.7 behaviour — plain
            ``[dynamic-config]`` lines on file descriptor 2, bypassing
            `logging` — for the deployment that greps stderr and wants
            no record objects anywhere. ``False`` turns the bridge back
            on.
    """
    if raw_stderr:
        _core._stop_log_bridge()
    else:
        _core._start_log_bridge()

    if level is not None:
        _core._set_engine_log_level(level)


# The forwarder takes the GIL per record; an interpreter that has begun
# finalising must never be attached to. `atexit` runs before finalisation,
# so stopping the bridge here closes that window — and the engine falls
# back to stderr for whatever a watcher says during shutdown itself.
atexit.register(_core._stop_log_bridge)
