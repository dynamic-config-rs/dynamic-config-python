"""`@dynamic_config`: the configuration attached to the model class.

For code that would rather import a class than be handed an object. It
attaches a `DynamicConfig` and the handful of methods that read through
it, and refuses a model that declares a field by one of those names.

**Inherit `Configured` if you type-check.** Attributes attached at
runtime are invisible to a type checker — Python has no way to say "this
class, plus these six members" — so `Database.current()` on a class that
only carries the decorator is an `attr-defined` error under mypy and
nothing at all to an editor's completion. `Configured` declares the six
where a checker can see them, and the decorator fills them in:

    @dynamic_config(key="db", files=["config.toml"])
    class Database(Configured, BaseModel):
        host: str = "localhost"

    Database.current().host        # typed as `str`, completes in an editor

The decorator alone still works, unchanged, for code that does not
type-check — but it cannot be made visible to one, and pretending
otherwise would be worse than saying it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional, TypeVar, cast

from ._config import DynamicConfig
from ._core import DynamicConfigError
from ._diagnostics import Explanation, Origin
from ._schema import field_names

if TYPE_CHECKING:
    # 3.11 has it in `typing`; every type checker resolves it from
    # `typing_extensions` on older ones, and this import never runs.
    from typing_extensions import Self

M = TypeVar("M")


class Configured:
    """What `@dynamic_config` attaches, declared so a checker can see it.

    A mixin with no runtime behaviour of its own beyond delegating to
    `config`, which the decorator sets. Inheriting it is what makes
    `Database.current()` type-check and complete in an editor; the
    methods below are the same ones the decorator would otherwise attach
    at runtime.

    It declares no fields, so a Pydantic model gains nothing but the
    methods — `model_fields` is untouched.
    """

    #: The configuration the decorator built. Set at decoration; reading
    #: it before then is an `AttributeError` that names this class.
    config: ClassVar[DynamicConfig[Any]]

    @classmethod
    def current(cls) -> Self:
        """The installed model. Raises before the first successful load."""
        return cast("Self", cls.config.current())

    @classmethod
    def try_current(cls) -> Optional[Self]:
        """The installed model, or ``None`` before the first load."""
        return cast("Optional[Self]", cls.config.try_current())

    @classmethod
    def reload(cls) -> None:
        """One reload: load, validate, install, rewrite the cache."""
        cls.config.reload()

    @classmethod
    def source_of(cls, path: str) -> Optional[Origin]:
        """Which layer would supply ``path``, or ``None``."""
        return cls.config.source_of(path)

    @classmethod
    def explain(cls, path: str) -> Explanation:
        """Every layer's answer for ``path``, secrets redacted."""
        return cls.config.explain(path)


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
    whole_document: bool = False,
    env_files: Sequence[str] = (),
    profile_env: str | None = None,
    cache: str | None = None,
    cache_mode: str = "redacted",
    init: bool = False,
    watch: float | None = None,
) -> Callable[[type[M]], type[M]]:
    """Attaches a configuration to a model class.

        @dynamic_config(key="db", files=["config.toml"], env="APP_")
        class Database(BaseModel):
            host: str
            port: int = 5432

        Database.config.init()
        Database.current()

    The decorator builds a :class:`DynamicConfig`, stores it as
    ``Model.config`` and attaches ``current``/``try_current``/``reload``/
    ``source_of``/``explain`` classmethods over it. Everything it does is
    something :class:`DynamicConfig` can be told directly; this is the
    declaration-shaped spelling of it, and every argument below is one
    fluent call.

    It does **not** load by default: import time is the wrong time to read
    files, and a script that disagrees passes ``init=True``. Decorating one
    class twice is an error, mirroring the crate's one-configuration-per-type
    rule.

    Every argument is keyword-only, and every one of them maps to a method
    on the configuration:

    Parameters:
        key: the section key — which top-level table of each document is
            this model's. Also names the environment prefix
            (``env="APP_"`` reads ``APP_{KEY}_*``), the cache entry and
            every diagnostic. Pass ``""`` for a configuration with nothing
            to call itself, together with ``whole_document=True``.
            :meth:`DynamicConfig.__init__`.
        files: files to merge, in order — later files win, a missing one
            is skipped, and the format comes from the extension.
            :meth:`DynamicConfig.file`.
        discover: ``(name, paths)``: look for ``{name}.{ext}`` in each
            directory, below anything ``files`` listed.
            :meth:`DynamicConfig.discover`.
        env: the environment prefix, trailing underscore included. The
            environment is read above every file.
            :meth:`DynamicConfig.env`.
        nest: the separator that means nesting inside a variable name;
            ``__`` unless given. :meth:`DynamicConfig.nest`.
        allow_empty_env: treat ``FOO=`` as set-to-empty rather than
            unset. :meth:`DynamicConfig.allow_empty_env`.
        strict_env: refuse ambiguous environment spellings — ``off``,
            ``no``, ``nil``. :meth:`DynamicConfig.strict_env`.
        whole_document: the documents carry **no section header**; each
            one *is* this model's values, as in
            ``{"host": "0.0.0.0", "port": 8000}``.
            :meth:`DynamicConfig.whole_document`.
        env_files: ``.env`` files, read as the environment layer and just
            below the real environment. :meth:`DynamicConfig.env_file`.
        profile_env: the variable naming the active profile, so
            ``config.toml`` gains ``config.production.toml``.
            :meth:`DynamicConfig.profile_env`.
        cache: where to write the last-known-good cache, or ``None`` for
            none. :meth:`DynamicConfig.cache`.
        cache_mode: ``"redacted"`` (the default), ``"full"`` or
            ``"fingerprint"``. Read only when ``cache`` is given.
        init: load at decoration time. ``False`` by default, because
            import time is the wrong time to read files — a script that
            disagrees says so here.
        watch: seconds of debounce for a file watcher started at
            decoration time, or ``None`` for no watcher. The watcher is
            detached — it runs for the life of the process — and it does
            not load: pair it with ``init=True`` for a class that has a
            snapshot from the first line and follows the file after.

    Returns:
        The decorator, which returns the same class with ``config`` and
        the classmethods attached.
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
        if whole_document:
            config.whole_document()
        for path in env_files:
            config.env_file(path)
        if profile_env is not None:
            config.profile_env(profile_env)
        if cache is not None:
            config.cache(cache, cache_mode)

        model.config = config  # type: ignore[attr-defined]

        # A class that inherits `Configured` already has the five readers,
        # typed, and they delegate through `config` — which was just set.
        # Attaching them again would shadow the typed ones with untyped
        # lambdas, which is the opposite of the point.
        if not issubclass(model, Configured):
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
