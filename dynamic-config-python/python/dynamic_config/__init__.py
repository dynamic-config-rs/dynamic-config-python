"""Hot-reloadable configuration: Rust resolves, your schema validates.

The engine is the `dynamic-config` Rust crate — sources, layering,
profiles, watching, last-known-good recovery and provenance. The schema
is a class you already know how to write. Validation runs once per
successful resolve, never per read, so ``current()`` is an attribute
lookup on a cached instance.

    from dataclasses import dataclass
    from dynamic_config import DynamicConfig

    @dataclass
    class Database:
        host: str = "localhost"
        port: int = 5432

    config = DynamicConfig(Database, key="db").file("config.toml").env("APP_")
    db = config.init_and_current()     # a Database, cached

The schema can be a `dataclasses.dataclass` (no dependencies), a Pydantic
model, a `pydantic_settings.BaseSettings` class or a `msgspec.Struct` —
`pip install dynamic-config-py[pydantic]`, `[pydantic-settings]` and
`[msgspec]` buy those, and `[all]` is the Pydantic pair. It can also be
`Values`, which is no schema at all: a configuration read by dotted path,
for keys a program learns at run time rather than declares.

This module is the public surface and nothing else. What it re-exports
lives next door, one concern per file:

    _config.py        the configuration object and its lifecycle
    _dataclasses.py   a plain dataclass as a schema — no dependencies
    _decorator.py     `@dynamic_config`, for settings on the class
    _diagnostics.py   what `explain`, `check` and `snapshot` hand back
    _errors.py        the one exception this side adds
    _executor.py      which pool pays for the blocking half
    _lifetime.py      `Watch`, `HookGuard`, and the shutdown sweep
    _msgspec.py       a msgspec Struct as a schema — imported only if used
    _pydantic.py      a Pydantic model as a schema — imported only if used
    _remote.py        `RemoteSource` and `Format`, for a store in Python
    _schema.py        which adapter a class gets, and the questions they all answer
    _settings.py      the pydantic-settings bridge
    _telemetry.py     `status()`, and the Prometheus exposition
    _values.py        `Values`, for a configuration with no schema class
    _core.pyi         stubs for the compiled half

Import from the package, not from those: a leading underscore is a
promise that the file may be reorganised, and this list is the only
thing that will not move.

Two neighbours have no underscore, because neither is imported from here at
all. `dynamic_config.pytest` is the pytest plugin, which pytest loads
through the package's `pytest11` entry point; it imports pytest and the
standard library and nothing else — including nothing from this module.
`dynamic_config.remote` is the door to the Rust remote stores, which are a
second wheel (`pip install dynamic-config-py[remote]`); importing it without
that wheel raises an ImportError naming the extra, and importing *this*
module never touches it.
"""

from __future__ import annotations

from . import _core
from ._config import DynamicConfig
from ._core import (
    AuthError,
    BackendError,
    DecryptError,
    DynamicConfigError,
    EnvError,
    InvalidError,
    IoError,
    MissingError,
    ParseError,
    RemoteError,
    TypeMismatchError,
)
from ._decorator import Configured, dynamic_config
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
from ._executor import configure_executor, executor, set_executor
from ._group import ConfigGroup
from ._lifetime import HookGuard, Watch
from ._logging import configure_logging
from ._remote import AsyncRemoteSource, Format, RemoteSource
from ._schema import secret_paths
from ._telemetry import ConfigStatus, Exposition, Failure, RemoteStatus
from ._values import Values

#: This package's version. It moves on its own schedule — see the
#: versioning note in the book: the wheel embeds the engine rather than
#: depending on a published version of it, so a Rust-only release is not
#: a reason to ask every Python user to upgrade.
__version__: str = _core.__version__

#: The `dynamic-config` Rust crate this wheel was built against.
__engine_version__: str = _core.__engine_version__

__all__ = [
    "AsyncRemoteSource",
    "AuthError",
    "BackendError",
    "Backpressure",
    "Change",
    "ConfigEvent",
    "ConfigGroup",
    "ConfigStatus",
    "Configured",
    "Contribution",
    "DecryptError",
    "Dispatch",
    "DynamicConfig",
    "DynamicConfigError",
    "EnvError",
    "Explanation",
    "Exposition",
    "Failure",
    "Format",
    "HookGuard",
    "InvalidError",
    "IoError",
    "MissingError",
    "NotInitialisedError",
    "Origin",
    "ParseError",
    "ReloadFailed",
    "Reloaded",
    "RemoteError",
    "RemoteSource",
    "RemoteStatus",
    "Report",
    "Resolved",
    "Snapshot",
    "TypeMismatchError",
    "UnknownKey",
    "Values",
    "Watch",
    "__engine_version__",
    "__version__",
    "changed_paths",
    "configure_executor",
    "configure_logging",
    "dynamic_config",
    "executor",
    "secret_paths",
    "set_executor",
]
