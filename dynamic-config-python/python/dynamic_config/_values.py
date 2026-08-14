"""A configuration with no schema class: read by path, at run time.

The Python half of the crate's `Dynamic<Value>`. A plugin host, a
feature-flag table, a tool inspecting somebody else's configuration — the
shapes where the keys are not known when the program is written, so there
is no model class to declare and nothing for a validator to check.

Everything else is unchanged, which is the point: the same layers,
profiles, discovery, secrets directory, watcher, last-known-good cache,
reload hooks, `source_of`, `explain` and `check`. What a schemaless
configuration gives up is exactly what it never declared — field names,
so `check()` reports no unknown keys and says so, and secret paths, which
`secrets=` supplies by hand for the cache modes that need them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


class Values(Mapping[str, Any]):
    """A resolved configuration with no model behind it.

    Both a marker and a result: pass the **class** to
    :class:`~dynamic_config.DynamicConfig` to say "this configuration has
    no schema", and every load hands back an **instance** of it holding
    what the sources resolved to.

        from dynamic_config import DynamicConfig, Values

        config = DynamicConfig(Values, key="plugins").file("plugins.toml")
        config.init()

        values = config.current()
        values["cache.ttl"]          # by dotted path
        values.get("cache.ttl", 60)  # ...with a default
        values["cache"]["ttl"]       # or a step at a time
        dict(values)                 # a plain dict, copied

    A `Mapping`, so `len()`, `in`, `.keys()`, `.items()` and iteration all
    work as they do on a dict, and every value is already a plain Python
    object: `str`, `int`, `float`, `bool`, `list`, `dict`, `None`. There
    is nothing to unwrap and no accessor to learn.

    **Lookup takes a dotted path**, which is the one place this is not a
    dict: ``values["db.host"]`` is ``values["db"]["host"]``. A top-level
    key with no dot in it behaves identically either way; a key that
    *contains* a dot is not reachable by name, which is the same trade the
    Rust `Value::get` makes and the same one every diagnostic in this
    library already makes by naming paths that way.

    Instances are immutable from the outside — a snapshot is a snapshot —
    and hash as their identity, not their contents.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        """Wraps ``data``, which is the resolved tree the loader produced.

        Parameters:
            data: the mapping to read from. ``None`` — the default — is an
                empty configuration, which is what a load that resolved
                nothing produces.

        Callers do not normally construct one: the engine builds it per
        load, exactly where a typed configuration builds its model. Doing
        it by hand is useful in a test that wants a configuration without
        a file behind it.
        """
        self._data: Mapping[str, Any] = {} if data is None else data

    # ── Mapping ────────────────────────────────────────────────────────

    def __getitem__(self, path: str) -> Any:
        """The value at ``path``, or `KeyError`.

        Parameters:
            path: a dotted path — ``"db.pool.max_size"`` — or a top-level
                key, which is the same thing with no dots in it.
        """
        found = self._walk(path)

        if found is _MISSING:
            raise KeyError(path)

        return found

    def __iter__(self) -> Iterator[str]:
        """The **top-level** keys, in the order the document had them."""
        return iter(self._data)

    def __len__(self) -> int:
        """How many top-level keys there are."""
        return len(self._data)

    def __contains__(self, path: object) -> bool:
        """Whether ``path`` — dotted or top-level — resolves to a value.

        Parameters:
            path: the dotted path to look for. Anything that is not a
                `str` is `False` rather than an error, as `Mapping`
                requires.
        """
        if not isinstance(path, str):
            return False

        return self._walk(path) is not _MISSING

    def get(self, path: str, default: Any = None) -> Any:
        """The value at ``path``, or ``default`` when nothing is there.

        Parameters:
            path: a dotted path, as :meth:`__getitem__` takes.
            default: what to answer when the path resolves to nothing.
                ``None`` unless given.
        """
        found = self._walk(path)

        return default if found is _MISSING else found

    def sub(self, path: str) -> Values:
        """The subtree at ``path``, as a `Values` of its own.

        What a subsystem gets handed instead of the whole configuration:
        below it, `sub("db")`'s paths are relative — ``pool.max_size``
        rather than ``db.pool.max_size`` — so a function that takes a
        `Values` does not have to know where in the document it lives.
        The Rust crate's :rust:`Snapshot::sub` is the same idea, and this
        is what a schemaless configuration was missing.

        ```python
        config = DynamicConfig(Values, key="app").file("config.toml")
        config.init()

        pool = config.current().sub("db.pool")

        pool["max_size"]        # not "db.pool.max_size"
        ```

        Parameters:
            path: a dotted path, as :meth:`__getitem__` takes.

        Returns:
            A `Values` over that subtree — **empty** when the path holds
            nothing, and empty when it holds a value rather than a table.

        The two empties are deliberate and are not an error: a subsystem
        handed a section its deployment did not configure should read its
        own defaults, not crash on the way to them. Ask
        :meth:`__contains__` first when the difference matters.
        """
        found = self._walk(path)

        return Values(found if isinstance(found, Mapping) else {})

    # ── Reading it as data ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """A plain `dict` of the whole configuration, one level deep.

        The nested values are the same objects this holds rather than
        copies, so it is cheap; they are the loader's own and nothing
        mutates them after a load.
        """
        return dict(self._data)

    def leaf_paths(self) -> list[str]:
        """Every dotted path that holds a value rather than a table.

        Sorted, and tables are not listed for their own sake: a
        configuration of ``{"db": {"host": "x"}}`` has one leaf,
        ``db.host``. The same list
        :meth:`~dynamic_config.Snapshot.leaf_paths` answers, from the
        installed model rather than from the resolved tree.
        """
        return sorted(_leaves(self._data, ""))

    def _walk(self, path: str) -> Any:
        """Follows a dotted ``path`` through the tree, or `_MISSING`."""
        current: Any = self._data

        for segment in path.split("."):
            if not isinstance(current, Mapping) or segment not in current:
                return _MISSING

            current = current[segment]

        return current

    def __repr__(self) -> str:
        """The top-level keys, and never a value.

        A schemaless configuration declares no secrets, so nothing here
        knows which values are sensitive — and a `repr` that printed them
        would be the leak `#[config(secret)]` exists to prevent, in the
        one configuration shape that cannot mark anything.
        """
        keys = ", ".join(sorted(self._data))

        return f"<Values keys=[{keys}]>"


class _Missing:
    """The sentinel for "no value here", distinct from a configured `None`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _leaves(data: Any, prefix: str) -> Iterator[str]:
    """Every dotted path under ``prefix`` that holds a value."""
    if isinstance(data, Mapping) and data:
        for key, value in data.items():
            yield from _leaves(value, f"{prefix}{key}.")

        return

    if prefix:
        yield prefix[:-1]


class ValuesSchema:
    """The adapter that makes :class:`Values` a schema like any other.

    It answers the same three questions every schema answers, and two of
    the answers are *nothing*: a schemaless configuration has no field
    names to compare a file against, and declares no secrets. Both are
    reported rather than assumed — `check()` renders ``unknown keys: not
    checked (no field list)`` instead of an empty list that would read as
    an all-clear, and a redacting cache mode is refused unless
    ``secrets=`` supplied the paths by hand.
    """

    __slots__ = ("_secrets", "model")

    def __init__(self, model: type, secrets: list[str] | None = None) -> None:
        """Wraps the marker class.

        Parameters:
            model: :class:`Values`, or a subclass of it.
            secrets: dotted paths whose values must never reach a
                diagnostic, supplied by the caller because there is no
                declaration to derive them from.
        """
        self.model = model
        self._secrets = list(secrets or [])

    @property
    def kind(self) -> str:
        """What to call this in a message."""
        return "schemaless configuration"

    def validate(self, data: Any) -> Any:
        """Wraps the resolved tree; there is nothing to check.

        Parameters:
            data: the mapping the loader resolved.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"a configuration resolves to a table, not {type(data).__name__}"
            )

        return self.model(data)

    def field_names(self) -> list[str]:
        """Nothing: a schemaless configuration names no fields in advance."""
        return []

    def secret_paths(self) -> list[str]:
        """The paths the caller declared with ``secrets=``, if any."""
        return list(self._secrets)

    def is_instance(self, value: Any) -> bool:
        """Whether ``value`` is one of these configurations.

        Parameters:
            value: the object to test.
        """
        return isinstance(value, self.model)
