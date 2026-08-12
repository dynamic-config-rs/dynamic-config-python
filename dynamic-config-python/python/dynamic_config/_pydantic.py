"""The Pydantic adapter: models, Pydantic dataclasses, every alias shape.

Imported only when a caller passes a Pydantic class — which is the one
situation in which Pydantic is certainly installed. `pip install
dynamic-config-py` alone never reaches this module.
"""

from __future__ import annotations

import dataclasses
import typing
from typing import Any

from pydantic import AliasChoices, AliasPath, BaseModel, RootModel

from ._schema import _unwrap, is_secret_type


def _alias_names(alias: Any) -> list[str]:
    """Every dotted key one alias declaration can mean.

    Pydantic accepts four shapes, and a field is reachable by all of
    them at once: a plain string, an ``AliasPath`` into nested data, an
    ``AliasChoices`` of either, and ``None``. A list rather than a single
    name because the answer genuinely is a list — a file may spell the
    field any of these ways, and something that must know *every* spelling
    (the redaction list) cannot pick one and hope.

    An ``AliasPath`` that indexes into a sequence — ``AliasPath("a", 0)``
    — has no dotted spelling, so it contributes the longest string prefix
    it does have. That is the ancestor of the value, which for redaction
    is the safe direction to be wrong in.
    """
    if alias is None:
        return []

    if isinstance(alias, str):
        return [alias]

    if isinstance(alias, AliasPath):
        parts: list[str] = []

        for element in alias.path:
            if not isinstance(element, str):
                break

            parts.append(element)

        return [".".join(parts)] if parts else []

    if isinstance(alias, AliasChoices):
        return [name for choice in alias.choices for name in _alias_names(choice)]

    return []


def keys_of(name: str, info: Any) -> list[str]:
    """Every name a configuration file could carry for this field.

    The field's own name and each alias it accepts. Deliberately
    generous: over-listing a name costs a key nothing supplies, while
    under-listing one costs a secret its redaction.
    """
    keys = [name]

    for alias in (
        getattr(info, "validation_alias", None),
        getattr(info, "alias", None),
    ):
        for key in _alias_names(alias):
            if key not in keys:
                keys.append(key)

    return keys


class _Annotated:
    """A stand-in ``FieldInfo`` for a plain dataclass field: a type, no alias."""

    __slots__ = ("annotation",)

    def __init__(self, annotation: Any) -> None:
        """Wraps a resolved annotation so field walking is one shape."""
        self.annotation = annotation


def fields_of(candidate: Any) -> dict[str, Any] | None:
    """The fields of anything Pydantic validates structurally, or ``None``.

    Models and Pydantic dataclasses both carry a mapping of ``FieldInfo``;
    a plain dataclass used as a field type carries only its annotations,
    which may still be strings under ``from __future__ import
    annotations`` and are resolved here. Anything else — a scalar, a
    container, a type Pydantic coerces — has no fields to walk.
    """
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return dict(candidate.model_fields)

    fields = getattr(candidate, "__pydantic_fields__", None)

    if isinstance(fields, dict):
        return dict(fields)

    if dataclasses.is_dataclass(candidate) and isinstance(candidate, type):
        try:
            hints = typing.get_type_hints(candidate)
        except Exception:  # pragma: no cover - unresolvable forward reference
            return None

        return {
            field.name: _Annotated(hints.get(field.name, field.type))
            for field in dataclasses.fields(candidate)
        }

    return None


def secret_paths(
    model: Any, _prefix: str = "", _seen: frozenset[Any] = frozenset()
) -> list[str]:
    """The dotted paths of every secret field in a Pydantic schema.

    Through ``Optional``, unions, containers, nested models, Pydantic
    dataclasses and ``RootModel``. Every name a file could use is listed,
    not one of them: a field with
    ``validation_alias=AliasChoices("password", "pass")`` is a secret
    under both spellings, and a file that picks the second must not be the
    one that reaches the diagnostics and the cache in the clear.
    """
    if model in _seen:
        # A self-referencing model: the paths below it repeat forever, and
        # a redaction list is about shapes, not depth.
        return []

    fields = fields_of(model)

    if fields is None:
        return []

    seen = _seen | {model}
    paths: list[str] = []

    for name, info in fields.items():
        keys = keys_of(name, info)

        for key in keys:
            paths.extend(_paths_under(info.annotation, f"{_prefix}{key}", seen))

    # Order is the model's, duplicates are not interesting to the engine.
    return list(dict.fromkeys(paths))


def _paths_under(annotation: Any, path: str, seen: frozenset[Any]) -> list[str]:
    """Every secret path one field contributes, at ``path``.

    Three shapes, and the difference between them is which *path* the
    engine will be asked about later:

    - a secret itself, or anything holding one inside a **container** —
      redacted at ``path``, whole. A `list[Credentials]` puts its
      passwords at `users.0.password`, and neither `touches_secret` nor
      the cache's `remove_path` has an indexed-path vocabulary; redacting
      the containing field is the answer that is wrong in the safe
      direction. Losing `users`'s non-secret siblings from a diagnostic
      costs a reader some context. Not losing them costs a password.
    - a nested model or dataclass — recursed into, under ``path``.
    - a `RootModel` — recursed into *at* ``path``, because its `root`
      field is synthetic and no file ever writes it.
    """
    if _holds_secret_in_container(annotation, seen):
        return [path]

    found: list[str] = []

    for candidate in _members(annotation):
        if is_secret_type(candidate):
            return [path]

        root = _root_annotation(candidate)

        if root is not None:
            # The data sits where the *outer* field is, so the root
            # annotation is walked at this path rather than under it.
            found.extend(_paths_under(root, path, seen | {candidate}))
            continue

        if fields_of(candidate) is not None:
            found.extend(secret_paths(candidate, f"{path}.", seen))

    return found


def _members(annotation: Any) -> list[Any]:
    """A union's members, or the annotation itself — containers intact."""
    origin = typing.get_origin(annotation)

    if origin is None:
        return [annotation]

    if _is_container(origin):
        # A container is not one of its members: descending into it here
        # is what produced `users.password` for a `list[Credentials]`.
        return []

    return [
        member
        for argument in typing.get_args(annotation)
        for member in _members(argument)
    ]


def _is_container(origin: Any) -> bool:
    """Whether an annotation's origin holds *many* values under one key."""
    return origin in {list, set, frozenset, tuple, dict} or (
        isinstance(origin, type)
        and issubclass(origin, (list, set, frozenset, tuple, dict))
    )


def _root_annotation(candidate: Any) -> Any | None:
    """A ``RootModel``'s content type, or ``None`` for anything else."""
    if (
        isinstance(candidate, type)
        and issubclass(candidate, RootModel)
        and candidate is not RootModel
    ):
        root = candidate.model_fields.get("root")

        return None if root is None else root.annotation

    return None


def _holds_secret_in_container(
    annotation: Any, seen: frozenset[Any], _depth: int = 0
) -> bool:
    """Whether a secret sits under a container in ``annotation``.

    Answered for the field as a whole rather than per path, because the
    answer decides whether the *field* is redacted. `_depth` guards a
    self-referencing model whose recursion `seen` cannot catch — the
    shapes here are types, not values, and a redaction list is about
    shapes.
    """
    if _depth > 8:  # pragma: no cover - a model nested past all reason
        return False

    origin = typing.get_origin(annotation)

    if origin is not None and _is_container(origin):
        return any(
            _contains_secret(argument, seen, _depth + 1)
            for argument in typing.get_args(annotation)
            if argument is not Ellipsis
        )

    return any(
        _holds_secret_in_container(argument, seen, _depth + 1)
        for argument in typing.get_args(annotation)
    )


def _contains_secret(annotation: Any, seen: frozenset[Any], depth: int = 0) -> bool:
    """Whether ``annotation`` is, or holds anywhere inside it, a secret."""
    if depth > 8:  # pragma: no cover - as above
        return False

    for candidate in _members(annotation):
        if is_secret_type(candidate):
            return True

        root = _root_annotation(candidate)

        if root is not None:
            if _contains_secret(root, seen, depth + 1):
                return True

            continue

        if candidate in seen:
            continue

        fields = fields_of(candidate)

        if fields is None:
            continue

        for info in fields.values():
            if _contains_secret(info.annotation, seen | {candidate}, depth + 1):
                return True

    # And the containers inside it, which `_members` deliberately skips.
    if typing.get_origin(annotation) is not None:
        return any(
            _contains_secret(argument, seen, depth + 1)
            for argument in typing.get_args(annotation)
            if argument is not Ellipsis
        )

    return False


def leaf_paths(model: Any, _seen: frozenset[Any] = frozenset()) -> list[list[str]]:
    """Every leaf field of ``model``, as the segments a file would write.

    Nested models are descended into because that is what
    pydantic-settings does with ``env_nested_delimiter``; containers are
    not, because a variable cannot name a list element.
    """
    if model in _seen:
        return []

    seen = _seen | {model}
    leaves: list[list[str]] = []

    for name, info in model.model_fields.items():
        key = keys_of(name, info)[0]
        nested = [
            candidate
            for candidate in _unwrap(info.annotation)
            if isinstance(candidate, type)
            and issubclass(candidate, BaseModel)
            and not issubclass(candidate, RootModel)
        ]

        if nested:
            for candidate in nested:
                leaves.extend([key, *below] for below in leaf_paths(candidate, seen))
        else:
            leaves.append([key])

    return leaves


class PydanticSchema:
    """A Pydantic model — or Pydantic dataclass — as a configuration schema."""

    __slots__ = ("model",)

    def __init__(self, model: type) -> None:
        """Wraps the class; everything else is read from it on demand."""
        self.model = model

    @property
    def kind(self) -> str:
        """What to call this in a message."""
        return "Pydantic model"

    def validate(self, data: Any) -> Any:
        """Validates through Pydantic's own entry point, once per resolve.

        `model_validate` for a model; the dataclass validator for a
        Pydantic dataclass, which has no such classmethod.
        """
        if isinstance(self.model, type) and issubclass(self.model, BaseModel):
            return self.model.model_validate(data)

        validator = self.model.__pydantic_validator__  # type: ignore[attr-defined]

        return validator.validate_python(data)

    def field_names(self) -> list[str]:
        """Every key a configuration file could use for a top-level field.

        Feeds the unknown-key report, so an accepted alias must be in it —
        a file spelling a field the way the model says it may is not an
        unknown key.
        """
        names: list[str] = []
        fields = fields_of(self.model) or {}

        for name, info in fields.items():
            for key in keys_of(name, info):
                if key not in names:
                    names.append(key)

        return names

    def secret_paths(self) -> list[str]:
        """Every dotted path whose value must not reach a diagnostic."""
        return secret_paths(self.model)

    def is_instance(self, value: Any) -> bool:
        """Whether ``value`` is an instance of this model."""
        return isinstance(value, self.model)
