"""A plain `dataclasses.dataclass` as a configuration schema.

For programs that do not want Pydantic in the dependency tree. The
dataclass is the declaration, exactly as a model would be:

    @dataclass
    class Database:
        host: str = "localhost"
        port: int = 5432
        password: str = field(default="", metadata={"secret": True})

What this adapter does is **structural validation**: every required field
present, no key the class has never heard of, nested dataclasses built
recursively, and each value checked against the type its field declares.
What it deliberately does not do is *coerce* — it is not a small Pydantic,
and pretending otherwise would be the kind of silent lie this library
exists to avoid:

- a value that is already the right type passes;
- an `Enum` field takes its member's value;
- a type that builds from one argument is built from it — `UUID`,
  `Path`, `Decimal`, `IPv4Address`;
- anything else that does not match its annotation is a validation
  failure naming the field and the two type names, never the value.

`datetime` is the case people meet first: a TOML file with a native date
gives you one, and a string in an environment variable does not become
one here. Declare it `str`, or use a Pydantic model — that is what
Pydantic is for, and `pip install dynamic-config-py[pydantic]` is the
whole of the difference.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from collections.abc import Mapping
from typing import Any

from ._schema import _unwrap, is_dataclass_type, is_secret_type

#: Python 3.10 spells a union `X | Y`, and the result is a `types.UnionType`
#: rather than a `typing.Union` — a distinction `typing.get_origin` does not
#: paper over. Fetched by name rather than imported: the import itself is
#: 3.10, and this package floors at 3.9.
_UNION_TYPE = getattr(types, "UnionType", None)

#: `field(metadata={SECRET: True})` — the stdlib's own extension point,
#: which is the natural place for a declaration Pydantic makes with a type.
SECRET = "secret"

#: Types that are their own parser: one positional argument, and the
#: constructor either builds the value or raises. Kept as a *behaviour*
#: rather than a list, so `UUID`, `Path`, `Decimal` and anything shaped
#: like them work without being enumerated here.
_UNPARSEABLE = (str, bytes, bool, int, float)


class ValidationError(ValueError):
    """What a dataclass schema raises when the data does not fit.

    Carries paths, expected types and what arrived *as a type name* —
    never a value, because this message travels into diagnostics.
    """


def _hints(model: type) -> dict[str, Any]:
    """The resolved annotations, or the raw ones if they cannot resolve."""
    try:
        return typing.get_type_hints(model)
    except Exception:  # pragma: no cover - unresolvable forward reference
        return {field.name: field.type for field in dataclasses.fields(model)}


def _required(field: dataclasses.Field[Any]) -> bool:
    """Whether a field has no default of any kind."""
    return (
        field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    )


def _name(annotation: Any) -> str:
    """A type's name for a message, without importing anything to get it."""
    return getattr(annotation, "__name__", None) or str(annotation)


def _members(annotation: Any) -> list[Any]:
    """A union's members, or the annotation itself — containers intact.

    Deliberately not `_unwrap`, which flattens *through* containers
    because a redaction list wants to see the `SecretStr` inside a
    `list[SecretStr]`. Validation wants the opposite: `list[int]` is a
    list, and forgetting that turns "expected list" into "expected int".
    """
    origin = typing.get_origin(annotation)

    if origin is typing.Union or (
        _UNION_TYPE is not None and isinstance(annotation, _UNION_TYPE)
    ):
        return [
            member
            for argument in typing.get_args(annotation)
            for member in _members(argument)
        ]

    if origin is not None and _is_annotated(annotation):
        # `Annotated[X, ...]` is X wearing metadata this adapter has no
        # opinion about.
        return _members(typing.get_args(annotation)[0])

    return [annotation]


def _is_annotated(annotation: Any) -> bool:
    """Whether an annotation is `Annotated[...]` rather than a container."""
    return type(annotation).__name__ == "_AnnotatedAlias"


def _coerce(annotation: Any, value: Any, path: str) -> Any:
    """One value against one annotation, structurally.

    Raises :class:`ValidationError` naming ``path`` and the types
    involved. Never includes the value: this message is a diagnostic, and
    the field it describes may be a password.
    """
    candidates = _members(annotation)

    # `Optional[X]` and every union containing `None`.
    if value is None:
        if any(candidate is type(None) for candidate in candidates):
            return None

        raise ValidationError(f"{path}: expected {_name(annotation)}, got None")

    concrete = [candidate for candidate in candidates if candidate is not type(None)]

    if not concrete or Any in concrete:
        return value

    failures: list[str] = []

    for candidate in concrete:
        try:
            return _one(candidate, value, path)
        except ValidationError as failure:
            failures.append(str(failure))

    # A union none of whose members fit: report the union, not each try.
    if len(concrete) > 1:
        raise ValidationError(
            f"{path}: expected {_name(annotation)}, got {_name(type(value))}"
        )

    raise ValidationError(failures[0])


def _one(candidate: Any, value: Any, path: str) -> Any:
    """``_coerce`` against a single, non-optional annotation."""
    origin = typing.get_origin(candidate)

    if origin is not None:
        # `list[str]`, `dict[str, int]`, `tuple[int, ...]` — check the
        # container, then each element against its parameter.
        return _container(candidate, origin, value, path)

    if is_dataclass_type(candidate):
        if not isinstance(value, Mapping):
            raise ValidationError(
                f"{path}: expected a table for {_name(candidate)}, "
                f"got {_name(type(value))}"
            )

        return build(candidate, value, f"{path}.")

    if isinstance(candidate, type) and issubclass(candidate, enum.Enum):
        try:
            return candidate(value)
        except ValueError:
            raise ValidationError(
                f"{path}: not one of {_name(candidate)}'s members"
            ) from None

    if not isinstance(candidate, type):
        # `Literal`, `Any`, a TypeVar — nothing to check structurally.
        return value

    # `bool` before `int`: `isinstance(True, int)` is true, and a flag
    # arriving as a number is exactly the confusion worth refusing.
    if candidate is bool:
        if isinstance(value, bool):
            return value

        raise ValidationError(f"{path}: expected bool, got {_name(type(value))}")

    if candidate is float and isinstance(value, int) and not isinstance(value, bool):
        # A file writing `1` for a float means 1.0; every format this
        # crate reads makes that distinction the same way.
        return float(value)

    if isinstance(value, candidate) and not (
        candidate is int and isinstance(value, bool)
    ):
        return value

    if is_secret_type(candidate):
        # A `SecretStr` annotation inside a dataclass, when Pydantic is
        # installed: it builds from the string it is given.
        return candidate(value)

    if candidate in _UNPARSEABLE:
        raise ValidationError(
            f"{path}: expected {_name(candidate)}, got {_name(type(value))}"
        )

    # `date`, `time` and `datetime` parse text through a classmethod
    # rather than their constructor, and a TOML file writing a native date
    # hands over exactly the text they want.
    parse = getattr(candidate, "fromisoformat", None)

    if parse is not None and isinstance(value, str):
        try:
            return parse(value)
        except ValueError:
            raise ValidationError(f"{path}: not a valid {_name(candidate)}") from None

    # `UUID("…")`, `Path("…")`, `Decimal("…")`, `IPv4Address("…")`: a type
    # that parses its own text does so here. One that does neither says
    # so, and says it without the value.
    try:
        return candidate(value)
    except Exception:
        raise ValidationError(
            f"{path}: expected {_name(candidate)}, got {_name(type(value))} "
            f"and {_name(candidate)} does not build from one"
        ) from None


def _container(annotation: Any, origin: Any, value: Any, path: str) -> Any:
    """A parameterised container, and its elements."""
    arguments = typing.get_args(annotation)

    if origin in (list, set, frozenset):
        if not isinstance(value, (list, tuple)):
            raise ValidationError(
                f"{path}: expected {_name(origin)}, got {_name(type(value))}"
            )

        items = [
            _coerce(arguments[0], item, f"{path}[{index}]") if arguments else item
            for index, item in enumerate(value)
        ]

        return origin(items)

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValidationError(f"{path}: expected tuple, got {_name(type(value))}")

        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(
                _coerce(arguments[0], item, f"{path}[{index}]")
                for index, item in enumerate(value)
            )

        if arguments and len(arguments) != len(value):
            raise ValidationError(
                f"{path}: expected {len(arguments)} element(s), got {len(value)}"
            )

        return tuple(
            _coerce(argument, item, f"{path}[{index}]")
            for index, (argument, item) in enumerate(zip(arguments, value))
        )

    if origin is dict:
        if not isinstance(value, Mapping):
            raise ValidationError(f"{path}: expected a table, got {_name(type(value))}")

        if len(arguments) != 2:
            return dict(value)

        return {
            key: _coerce(arguments[1], item, f"{path}.{key}")
            for key, item in value.items()
        }

    if origin is typing.Literal:
        allowed = typing.get_args(annotation)

        # `Literal` is a set of *values*, so this is the one check that
        # compares against them rather than against a type. `True` and `1`
        # compare equal in Python and are different configuration values,
        # so the type has to match too.
        if any(item == value and type(item) is type(value) for item in allowed):
            return value

        raise ValidationError(
            f"{path}: not one of the {len(allowed)} values this field allows"
        )

    # `Annotated` and friends: what is left is not a structure to check.
    return value


def build(model: type, data: Mapping[str, Any], prefix: str = "") -> Any:
    """Builds ``model`` from ``data``, or raises :class:`ValidationError`."""
    fields = {field.name: field for field in dataclasses.fields(model)}
    hints = _hints(model)

    unknown = [key for key in data if key not in fields]

    if unknown:
        # The same refusal a dataclass constructor gives for an unexpected
        # keyword, made a validation failure so it reads like one.
        raise ValidationError(
            f"{prefix}{unknown[0]}: {_name(model)} declares no such field"
        )

    missing = [
        name for name, field in fields.items() if _required(field) and name not in data
    ]

    if missing:
        raise ValidationError(
            f"{prefix}{missing[0]}: required, and nothing supplies it"
        )

    arguments = {
        name: _coerce(hints.get(name, field.type), data[name], f"{prefix}{name}")
        for name, field in fields.items()
        if name in data
    }

    return model(**arguments)


def secret_paths(
    model: type, _prefix: str = "", _seen: frozenset[Any] = frozenset()
) -> list[str]:
    """Every dotted path a dataclass declares as secret.

    Two declarations count, and they mean the same thing: the stdlib's own
    ``field(metadata={"secret": True})``, and a `SecretStr`/`SecretBytes`
    annotation for a program that has Pydantic installed and would rather
    say it with a type.
    """
    if model in _seen:
        return []

    seen = _seen | {model}
    paths: list[str] = []
    hints = _hints(model)

    for field in dataclasses.fields(model):
        path = f"{_prefix}{field.name}"
        annotation = hints.get(field.name, field.type)

        if field.metadata.get(SECRET):
            paths.append(path)
            continue

        for candidate in _unwrap(annotation):
            if is_secret_type(candidate):
                paths.append(path)
                break

            if is_dataclass_type(candidate):
                paths.extend(secret_paths(candidate, f"{path}.", seen))

    return list(dict.fromkeys(paths))


class DataclassSchema:
    """A plain dataclass as a configuration schema."""

    __slots__ = ("model",)

    def __init__(self, model: type) -> None:
        """Wraps the class; everything else is read from it on demand."""
        self.model = model

    @property
    def kind(self) -> str:
        """What to call this in a message."""
        return "dataclass"

    def validate(self, data: Any) -> Any:
        """Builds an instance, structurally, once per resolve."""
        if not isinstance(data, Mapping):
            raise ValidationError(
                f"expected a table for {_name(self.model)}, got {_name(type(data))}"
            )

        return build(self.model, data)

    def field_names(self) -> list[str]:
        """Every key a configuration file may use — a dataclass has no aliases."""
        return [field.name for field in dataclasses.fields(self.model)]

    def secret_paths(self) -> list[str]:
        """Every dotted path whose value must not reach a diagnostic."""
        return secret_paths(self.model)

    def is_instance(self, value: Any) -> bool:
        """Whether ``value`` is an instance of this dataclass."""
        return isinstance(value, self.model)
