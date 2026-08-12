"""`@dynamic_config`: the configuration attached to the model class.

For code that would rather import a class than be handed an object. It
attaches a `DynamicConfig` and the handful of methods that read through
it, and refuses a model that declares a field by one of those names.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Callable, TypeVar

from ._config import DynamicConfig
from ._core import DynamicConfigError
from ._schema import field_names

M = TypeVar("M")

#: What the decorator puts on the class. A model declaring a field by one
#: of these names would have it shadowed, so that is refused rather than
#: silently overwritten.
ATTACHED = ("config", "current", "try_current", "reload", "source_of", "explain")


# ── The decorator ──────────────────────────────────────────────────────


def dynamic_config(
    *,
    key: str,
    files: Sequence[str] = (),
    discover: tuple[str, Sequence[str]] | None = None,
    env: str | None = None,
    nest: str | None = None,
    allow_empty_env: bool = False,
    strict_env: bool = False,
    env_files: Sequence[str] = (),
    profile_env: str | None = None,
    cache: str | None = None,
    cache_mode: str = "redacted",
    init: bool = False,
    watch: float | None = None,
) -> Callable[[type[M]], type[M]]:
    """Attaches a configuration to a Pydantic model class.

        @dynamic_config(key="db", files=["config.toml"], env="APP_")
        class Database(BaseModel):
            host: str
            port: int = 5432

        Database.config.init()
        Database.current()

    The decorator builds a :class:`DynamicConfig`, stores it as
    ``Model.config`` and attaches ``current``/``try_current``/``reload``/
    ``source_of``/``explain`` classmethods over it.

    It does **not** load by default: import time is the wrong time to read
    files, and a script that disagrees passes ``init=True``. Decorating one
    class twice is an error, mirroring the crate's one-configuration-per-type
    rule.
    """

    def decorate(model: type[M]) -> type[M]:
        """Attaches the configuration and the classmethods over it."""
        if "config" in vars(model):
            raise DynamicConfigError(
                f"{model.__name__} already has a configuration attached; "
                "one declaration per class"
            )

        # The decorator hangs six names on the class. A model that
        # declares a field called `config` or `reload` would have them
        # shadowed at class level and nowhere else — the kind of collision
        # that reads as a Pydantic bug three files away. Refuse it here,
        # where the cause is on screen.
        collisions = [name for name in ATTACHED if name in set(field_names(model))]

        if collisions:
            raise DynamicConfigError(
                f"{model.__name__} declares {', '.join(collisions)}, which the "
                "decorator would shadow; use DynamicConfig(...) directly for "
                "this model"
            )

        config: DynamicConfig[M] = DynamicConfig(model, key)

        for path in files:
            config.file(path)
        if discover is not None:
            config.discover(discover[0], discover[1])
        if env is not None:
            config.env(env)
        if nest is not None:
            config.nest(nest)
        if allow_empty_env:
            config.allow_empty_env()
        if strict_env:
            config.strict_env()
        for path in env_files:
            config.env_file(path)
        if profile_env is not None:
            config.profile_env(profile_env)
        if cache is not None:
            config.cache(cache, cache_mode)

        model.config = config  # type: ignore[attr-defined]
        model.current = classmethod(lambda _cls: config.current())  # type: ignore[attr-defined]
        model.try_current = classmethod(lambda _cls: config.try_current())  # type: ignore[attr-defined]
        model.reload = classmethod(lambda _cls: config.reload())  # type: ignore[attr-defined]
        model.source_of = classmethod(  # type: ignore[attr-defined]
            lambda _cls, path: config.source_of(path)
        )
        model.explain = classmethod(  # type: ignore[attr-defined]
            lambda _cls, path: config.explain(path)
        )

        if init:
            config.init()
        if watch is not None:
            config.watch(watch).detach()

        return model

    return decorate
