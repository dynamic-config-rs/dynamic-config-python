"""A `msgspec.Struct` as a configuration schema.

The fifth kind of declaration this binding accepts, and the fastest one:
msgspec builds an instance from a mapping in C, which is exactly the
shape of work a reload asks of a schema — one resolved mapping, one
instance, once.

    import msgspec

    class Database(msgspec.Struct):
        host: str
        port: int = 5432

The declaration reads like the others, and everything around it is
unchanged: the same layers, the same watcher, the same diagnostics.
What differs is what *validation* means, and the three places msgspec
answers differently from the adapters beside it are all here rather
than in a caller's surprise:

**Secrets are declared through `Meta(extra=...)`.** Pydantic has
`SecretStr` and a dataclass has ``field(metadata={"secret": True})``;
msgspec has neither, but its `Meta` carries an ``extra`` mapping for
exactly this — another library's flag on a field. So::

    password: Annotated[str, msgspec.Meta(extra={"secret": True})] = ""

drives the same redaction the other two do: the cache drops it,
`explain` renders it ``***``. ``DynamicConfig(Model, key=…,
secrets=[…])`` still adds to that list, as it does everywhere.

**Unknown keys are the declaration's business.** `msgspec.convert`
ignores a key the struct never declared, the way a Pydantic model does;
``class Database(msgspec.Struct, forbid_unknown_fields=True)`` refuses
it, the way this package's dataclass adapter always does. Neither is
imposed here — `check()` reports unknown keys either way.

**A field has exactly one name here.** The Pydantic adapter walks a
field under every spelling a file could use, because an alias there is
an *additional* way in: the Python name keeps working. `rename=` on a
struct is a replacement — ``rename="camel"`` makes ``max_size``
undecodable, and a file writing it is reporting an unknown key rather
than setting the field. So `encode_name` alone is exact for secrets and
for diffs, and asking for more names would be redacting paths no source
can produce.

**`InvalidError.errors` is empty.** msgspec raises a `ValidationError`
with a message and no structured report, so there is nothing to put
there and nothing is invented; `str(error)` names the field, as it does
for a dataclass schema.

Imported only when a caller passes a `Struct` subclass, which is the one
situation in which msgspec is certainly installed.
"""

from __future__ import annotations

import typing
from typing import Any, Optional

import msgspec
from msgspec import ValidationError

from ._schema import _unwrap, is_msgspec_struct

#: The `Meta(extra=...)` key that marks a field secret — the same word the
#: dataclass adapter reads out of `field(metadata=...)`, because it is the
#: same declaration wearing the vocabulary of a different library.
SECRET = "secret"

#: What msgspec puts between a message and the path it happened at.
_AT = " - at "

#: The two message shapes msgspec writes the offending *value* into:
#: ``Invalid enum value 'x'`` for an enum or a `Literal`, and ``Invalid
#: value 'x'`` for a tagged union's tag. Every other message it writes
#: names types, paths and declared constraints — never the data.
_QUOTES_THE_VALUE = {
    "Invalid enum value": "not one of the values this field allows",
    "Invalid value": "not one of the tags this union allows",
}


def _metas(annotation: Any) -> list[msgspec.Meta]:
    """Every `Meta` attached to an annotation, through unions and `Annotated`."""
    return [
        candidate
        for candidate in _unwrap(annotation)
        if isinstance(candidate, msgspec.Meta)
    ]


def _is_secret(annotation: Any) -> bool:
    """Whether any `Meta` on this annotation declares the field secret."""
    return any((meta.extra or {}).get(SECRET) for meta in _metas(annotation))


def _value_at(data: Any, path: str) -> Any:
    """The value a msgspec path points at, or `None` if it does not resolve.

    msgspec writes ``$.database.hosts[0]`` — the file's own keys, plus an
    index for a sequence. A dict *key* failure reads ``$.mapping[...]``
    and a mapping key is not a value, so both give up here and the
    prefix rule below is what covers them.
    """
    if not path.startswith("$"):
        return None

    segments = _segments(path[1:])

    if segments is None:
        return None

    current = data

    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                return None

            current = current[segment]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None

    return current


def _segments(path: str) -> Optional[list[str]]:
    """``.a.b[0]`` as ``["a", "b", "0"]``, or `None` if a step was elided."""
    segments: list[str] = []
    current = ""

    for character in path:
        if character in ".[]":
            if current == "...":
                # `$.mapping[...]`: msgspec elides the key it failed on,
                # so there is no value to walk to.
                return None

            if current:
                segments.append(current)

            current = ""
        else:
            current += character

    if current:
        segments.append(current)

    return segments


def scrub(message: str, data: Any) -> str:
    """One msgspec message with the offending value taken back out.

    This boundary's rule is that a value never reaches a diagnostic, and
    msgspec's rule is not quite that: two of its messages quote the data
    they refused. Both halves are handled, in the order that keeps the
    most of the original message:

    1. the two known shapes are replaced outright, which also covers the
       paths that do not resolve;
    2. whatever value the reported path *does* resolve to is taken out of
       what is left, so a message shape msgspec grows later cannot leak
       through this the way it would through a list of prefixes.

    The path is kept in both cases: it is made of field names, which are
    the declaration rather than the data, and it is what a person needs
    to find the field.
    """
    head, separator, path = message.rpartition(_AT)

    if not separator:
        head, path = message, ""

    for prefix, replacement in _QUOTES_THE_VALUE.items():
        if head.startswith(prefix):
            head = replacement
            break
    else:
        value = _value_at(data, path.strip("`"))

        if value is not None:
            for rendering in (repr(value), str(value)):
                if len(rendering) > 1 and rendering in head:
                    head = head.replace(rendering, "…")

    return f"{head}{separator}{path}" if separator else head


def secret_paths(
    model: type, _prefix: str = "", _seen: frozenset[Any] = frozenset()
) -> list[str]:
    """Every dotted path a `Struct` declares as secret.

    Under the names a *file* writes — `encode_name`, which is the field's
    own name unless `rename` or ``msgspec.field(name=…)`` moved it. Nested
    structs are descended into, the way the Pydantic walk descends into
    nested models.
    """
    if model in _seen:
        return []

    seen = _seen | {model}
    paths: list[str] = []

    for field in msgspec.structs.fields(model):
        path = f"{_prefix}{field.encode_name}"

        if _is_secret(field.type):
            paths.append(path)
            continue

        # A struct *directly* under this field is descended into: the
        # redaction walks tables, so `credentials.password` is a path it
        # can reach.
        nested = [
            candidate
            for candidate in _unwrap(field.type)
            if is_msgspec_struct(candidate)
        ]

        if not nested:
            continue

        if _under_a_container(field.type):
            # A struct inside a *container* — `list[Credentials]`,
            # `dict[str, Credentials]` — is a different case, and the one
            # that used to leak: `users.password` names nothing the
            # redaction can walk to, because it would have to index a
            # list. So the **containing field** is redacted whole, which
            # is what the Pydantic and dataclass adapters do and is the
            # safe direction to be wrong in.
            if any(secret_paths(candidate, "", seen) for candidate in nested):
                paths.append(path)

            continue

        for candidate in nested:
            paths.extend(secret_paths(candidate, f"{path}.", seen))

    return list(dict.fromkeys(paths))


def _under_a_container(annotation: Any) -> bool:
    """Whether a struct in this annotation sits inside a list, dict or tuple.

    `Optional[Credentials]` is not one: `None` or a table, and a table is
    walkable. `list[Credentials]` is, and so is `dict[str, Credentials]`.
    """
    origin = typing.get_origin(annotation)

    if origin in (list, set, frozenset, tuple, dict):
        return True

    return any(
        _under_a_container(argument)
        for argument in typing.get_args(annotation)
        if argument is not Ellipsis
    )


class MsgspecSchema:
    """A `msgspec.Struct` as a configuration schema."""

    __slots__ = ("model",)

    def __init__(self, model: type) -> None:
        """Wraps the class; everything else is read from it on demand."""
        self.model = model

    @property
    def kind(self) -> str:
        """What to call this in a message."""
        return "msgspec Struct"

    def validate(self, data: Any) -> Any:
        """Builds an instance through `msgspec.convert`, once per resolve.

        ``strict=False`` because configuration arrives as text more often
        than not — an environment variable is a string, and a TOML
        integer meeting a `float` field is the same widening every other
        schema here performs. Strict decoding would refuse `APP_DB_PORT`
        for being a `str`, which is not a mistake anybody made.
        """
        try:
            return msgspec.convert(data, self.model, strict=False)
        except ValidationError as failure:
            raise ValidationError(scrub(str(failure), data)) from None

    def field_names(self) -> list[str]:
        """Every key a configuration file may use for a top-level field.

        `encode_name` rather than `name`: that is the key msgspec itself
        decodes, so a renamed field is unknown under its Python name and
        the unknown-key report should say so.
        """
        return [field.encode_name for field in msgspec.structs.fields(self.model)]

    def secret_paths(self) -> list[str]:
        """Every dotted path whose value must not reach a diagnostic."""
        return secret_paths(self.model)

    def is_instance(self, value: Any) -> bool:
        """Whether ``value`` is an instance of this struct."""
        return isinstance(value, self.model)
