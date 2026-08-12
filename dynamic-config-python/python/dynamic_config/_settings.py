"""pydantic-settings: the half that is a schema, and the half this replaces.

`BaseSettings` is a Pydantic model bolted to a set of places to read it
from. The model half works here unchanged. The sourcing half does not run
under `model_validate` — which is how this binding validates — so a class
declaring `env_prefix` would quietly get none of it.

Quietly is the part that is not acceptable. These helpers let
`DynamicConfig` warn about such a declaration and let `from_settings`
honour it by translating it into engine sources.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# ── pydantic-settings, whose sourcing this engine replaces ─────────────
#
# `BaseSettings` is two things bolted together: a Pydantic model, and a
# set of places to read it from. The model half is welcome — it is a
# `BaseModel` and works here unchanged. The sourcing half overlaps this
# crate's entire job, and it does not run under `model_validate`, which
# is how this binding validates. So a settings class whose
# `SettingsConfigDict` declares sources would quietly get none of them.
#
# Quietly is the part that is not acceptable. `DynamicConfig` warns when
# it sees such a declaration, and `from_settings` honours what can be
# honoured by translating it into engine sources.

#: The `SettingsConfigDict` keys that choose *where values come from*,
#: each with the value that means "not declared".
_SETTINGS_SOURCING = {
    "env_prefix": "",
    "env_file": None,
    "env_nested_delimiter": None,
    "secrets_dir": None,
    "toml_file": None,
    "json_file": None,
    "yaml_file": None,
    "cli_parse_args": None,
}


def _is_settings(model: Any) -> bool:
    """Whether ``model`` is a ``pydantic_settings.BaseSettings`` subclass.

    Asked without importing pydantic-settings: it is an optional
    dependency, and a package that imports it to answer a question about
    a model that is not one would be requiring it of everybody.
    """
    return any(
        base.__module__.startswith("pydantic_settings")
        and base.__name__ == "BaseSettings"
        for base in type.mro(model)
    )


def _declared_sourcing(model: Any) -> dict[str, Any]:
    """Which sourcing options a settings class declares, and as what."""
    config = getattr(model, "model_config", {}) or {}

    declared = {
        name: config[name]
        for name, absent in _SETTINGS_SOURCING.items()
        if config.get(name, absent) != absent
    }

    if "settings_customise_sources" in vars(model):
        declared["settings_customise_sources"] = "overridden"

    return declared


def _as_paths(declared: Any) -> list[str]:
    """One path, several, or none — as pydantic-settings allows for each."""
    if declared is None:
        return []

    if isinstance(declared, (str, bytes)) or not isinstance(declared, Iterable):
        return [str(declared)]

    return [str(path) for path in declared]


def _leaf_paths(model: Any) -> list[list[str]]:
    """Every leaf field of ``model``, as the segments a file would write.

    Delegated to the Pydantic walk: only a settings class reaches this,
    and a settings class is a Pydantic model by construction — so the
    import is safe exactly where it happens.
    """
    from ._pydantic import leaf_paths

    return leaf_paths(model)
