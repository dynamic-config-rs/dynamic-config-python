"""What a schema class says about itself, read once at construction.

A *schema* here is whatever a caller declares their configuration with.
Four of them ship — a Pydantic model, a plain `dataclasses.dataclass`, a
`msgspec.Struct`, and `Values` for no schema at all — and each answers
the same questions, so that nothing else in this package knows which one
it is holding:

    validate(mapping)  →  an instance, or an exception naming what is wrong
    field_names()      →  every key a configuration file may use
    secret_paths()     →  every path that must never reach a diagnostic

Pydantic and msgspec are **optional** dependencies. Nothing in this
module imports either; each adapter lives in its own file and is
imported only when a caller passes a class of that kind, which is the one
situation in which the library is certainly installed. A dataclass user
never pays for either.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from typing import Protocol

    class Schema(Protocol):
        """What every schema adapter answers."""

        model: type

        # Each body is an explicit `...` rather than the docstring alone:
        # pyright reads a docstring-only body as a function that falls off
        # the end returning `None`, and reports four `reportReturnType`
        # errors a user's Pylance would show them too. mypy does not, which
        # is exactly why both run in the gate.
        @property
        def kind(self) -> str:
            """What to call this in a message: "Pydantic model", "dataclass"."""
            ...

        def validate(self, data: Any) -> Any:
            """Builds an instance, or raises describing what is wrong."""
            ...

        def field_names(self) -> list[str]:
            """Every key a configuration file may use for a top-level field."""
            ...

        def secret_paths(self) -> list[str]:
            """Every dotted path whose value must not reach a diagnostic."""
            ...

        def is_instance(self, value: Any) -> bool:
            """Whether ``value`` is an instance of this schema."""
            ...


def _unwrap(annotation: Any) -> list[Any]:
    """Every type an annotation could be: through ``Optional`` and unions."""
    origin = typing.get_origin(annotation)

    if origin is None:
        return [annotation]

    # `Optional[X]`, `X | None`, `Union[A, B]`, `Annotated[X, ...]`.
    return [
        candidate
        for argument in typing.get_args(annotation)
        for candidate in _unwrap(argument)
    ]


def is_secret_type(candidate: Any) -> bool:
    """Whether an annotation is one of Pydantic's secret wrappers.

    Asked by name rather than by import: this runs for dataclass schemas
    too, and a package that imported Pydantic to answer a question about a
    dataclass would be requiring it of everybody.
    """
    return (
        isinstance(candidate, type)
        and getattr(candidate, "__module__", "").startswith("pydantic")
        and candidate.__name__ in {"SecretStr", "SecretBytes"}
    )


def is_pydantic_model(candidate: Any) -> bool:
    """Whether ``candidate`` is a ``pydantic.BaseModel`` subclass.

    By walking the MRO for the name, so this costs nothing and imports
    nothing when Pydantic is not installed.
    """
    if not isinstance(candidate, type):
        return False

    return any(
        base.__name__ == "BaseModel" and base.__module__.startswith("pydantic")
        for base in type.mro(candidate)
    )


def is_pydantic_dataclass(candidate: Any) -> bool:
    """Whether ``candidate`` is a Pydantic dataclass rather than a plain one."""
    return isinstance(candidate, type) and hasattr(candidate, "__pydantic_validator__")


def is_msgspec_struct(candidate: Any) -> bool:
    """Whether ``candidate`` is a `msgspec.Struct` subclass.

    By the name in its MRO, for the same reason the Pydantic check is:
    this runs for every declaration, and a package that imported msgspec
    to find out it was not one would be requiring it of everybody.
    """
    if not isinstance(candidate, type):
        return False

    return any(
        base.__name__ == "Struct" and base.__module__.startswith("msgspec")
        for base in type.mro(candidate)
    )


def is_dataclass_type(candidate: Any) -> bool:
    """Whether ``candidate`` is a dataclass *class* — not an instance of one."""
    return isinstance(candidate, type) and dataclasses.is_dataclass(candidate)


def is_values_type(candidate: Any) -> bool:
    """Whether ``candidate`` is `Values`, or a subclass of it."""
    from ._values import Values

    return isinstance(candidate, type) and issubclass(candidate, Values)


def schema_for(model: Any, secrets: Sequence[str] | None = None) -> Schema:
    """The adapter for whatever kind of schema ``model`` is.

    Parameters:
        model: the class the configuration was declared with — a Pydantic
            model, a plain dataclass, a `msgspec.Struct`, or `Values` for
            a configuration with no schema at all.
        secrets: dotted paths whose values must never reach a diagnostic.
            Only a schemaless configuration has any use for them: a
            declared model says which of its own fields are secret, and
            this is how the one that cannot declare says it.

    `Values` first, because it is none of the others; then Pydantic,
    because a Pydantic dataclass is also a dataclass and the one that
    validates is the one to use; then msgspec, before the dataclass check
    — a `Struct` is not a dataclass, but check order is where this kind
    of bug lives, and being explicit costs one comment.
    """
    if is_values_type(model):
        from ._values import ValuesSchema

        return ValuesSchema(model, list(secrets or ()))

    if is_pydantic_model(model) or is_pydantic_dataclass(model):
        from ._pydantic import PydanticSchema

        return PydanticSchema(model)

    if is_msgspec_struct(model):
        from ._msgspec import MsgspecSchema

        return MsgspecSchema(model)

    if is_dataclass_type(model):
        from ._dataclasses import DataclassSchema

        return DataclassSchema(model)

    raise TypeError(
        "a configuration needs a schema class — a Pydantic model, a "
        "dataclass, a msgspec Struct, or Values for one with no schema "
        f"— not {model!r}"
    )


def secret_paths(model: Any) -> list[str]:
    """The dotted paths of every secret field in ``model``.

    Derived from the declaration itself, so nobody keeps a second list of
    what is sensitive in step with the first. For a Pydantic model that
    means `SecretStr`/`SecretBytes` fields, under every name a file could
    use for them; for a dataclass it means fields marked
    ``field(metadata={"secret": True})``, and the same wrappers if
    Pydantic happens to be installed and used; for a `msgspec.Struct` it
    means ``Annotated[str, msgspec.Meta(extra={"secret": True})]``, which
    is the same declaration in that library's own vocabulary.

    This is what seeds the redaction: the last-known-good cache drops
    those paths, `explain` renders them `***`, and a scrubbed validation
    error keeps their locations without their values.
    """
    return schema_for(model).secret_paths()


def field_names(model: Any) -> list[str]:
    """Every key a configuration file could use for ``model``'s fields."""
    return schema_for(model).field_names()


def validator_for(model: Any) -> Callable[[Any], Any]:
    """The callable the engine hands a resolved mapping, once per resolve."""
    return schema_for(model).validate
