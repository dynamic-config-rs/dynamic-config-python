"""Hot-reloadable configuration: Rust resolves, Pydantic validates.

The engine is the `dynamic-config` Rust crate — sources, layering,
profiles, watching, last-known-good recovery and provenance. The schema
is a Pydantic model you already know how to write. Validation runs once
per successful resolve, never per read, so ``current()`` is an attribute
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
model or a `pydantic_settings.BaseSettings` class — `pip install
dynamic-config-py[pydantic]` and `[pydantic-settings]` buy those, `[all]`
buys both.

This module is the public surface and nothing else. What it re-exports
lives next door, one concern per file:

    _config.py        the configuration object and its lifecycle
    _dataclasses.py   a plain dataclass as a schema — no dependencies
    _decorator.py     `@dynamic_config`, for settings on the class
    _diagnostics.py   what `explain`, `check` and `snapshot` hand back
    _errors.py        the one exception this side adds
    _executor.py      which pool pays for the blocking half
    _lifetime.py      `Watch`, `HookGuard`, and the shutdown sweep
    _pydantic.py      a Pydantic model as a schema — imported only if used
    _schema.py        which adapter a class gets, and the questions both answer
    _settings.py      the pydantic-settings bridge
    _core.pyi         stubs for the compiled half

Import from the package, not from those: a leading underscore is a
promise that the file may be reorganised, and this list is the only
thing that will not move.
"""

from __future__ import annotations

from . import _core
from ._config import DynamicConfig
from ._core import (
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
from ._decorator import dynamic_config
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
from ._executor import set_executor
from ._lifetime import HookGuard, Watch
from ._schema import secret_paths

#: This package's version. It moves on its own schedule — see the
#: versioning note in the book: the wheel embeds the engine rather than
#: depending on a published version of it, so a Rust-only release is not
#: a reason to ask every Python user to upgrade.
__version__: str = _core.__version__

#: The `dynamic-config` Rust crate this wheel was built against.
__engine_version__: str = _core.__engine_version__

__all__ = [
    "BackendError",
    "Change",
    "Contribution",
    "DecryptError",
    "DynamicConfig",
    "DynamicConfigError",
    "EnvError",
    "Explanation",
    "HookGuard",
    "InvalidError",
    "IoError",
    "MissingError",
    "NotInitialisedError",
    "Origin",
    "ParseError",
    "RemoteError",
    "Report",
    "Resolved",
    "Snapshot",
    "TypeMismatchError",
    "UnknownKey",
    "Watch",
    "__engine_version__",
    "__version__",
    "changed_paths",
    "dynamic_config",
    "secret_paths",
    "set_executor",
]
