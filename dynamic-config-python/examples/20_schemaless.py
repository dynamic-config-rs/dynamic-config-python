"""A configuration with no model class: `Values`.

python examples/20_schemaless.py

For the keys a program learns at run time rather than declares — a plugin
host, a feature-flag table, a tool reading somebody else's file. Pass the
`Values` class instead of a model and every load hands back a `Mapping`
read by dotted path.

Everything else is unchanged: the same layers, the same diagnostics, the
same watcher. What a schemaless configuration gives up is exactly what it
never declared — field names, so `check()` says it compared nothing, and
secret paths, which `secrets=` supplies by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from _shared import show
from dynamic_config import BackendError, DynamicConfig, Values

PLUGINS = {
    "plugins": {
        "cache": {"backend": "redis", "ttl": 60},
        "enabled": ["metrics", "tracing"],
        "token": "hunter2",
    }
}


def reading(path: Path) -> None:
    """A model would have had to name these keys in advance."""
    show("reading by path")

    config = DynamicConfig(Values, key="plugins").file(str(path))
    config.init()

    values = config.current()

    print(f"  values['cache.ttl']          {values['cache.ttl']}")
    print(f"  values['cache']['backend']   {values['cache']['backend']}")
    print(f"  values.get('cache.size', 8)  {values.get('cache.size', 8)}")
    print(f"  values['enabled']            {values['enabled']}")
    print(f"  len(values), sorted(values)  {len(values)}, {sorted(values)}")
    print(f"  'cache.ttl' in values        {'cache.ttl' in values}")
    print(f"  values.leaf_paths()          {values.leaf_paths()}")
    print(f"  repr(values)                 {values!r}")
    print("\n  The repr carries keys and never a value: nothing here knows")
    print("  which of them is a credential, so none of them prints.")


def the_layers_are_the_same(path: Path) -> None:
    """No schema does not mean no engine."""
    show("the layers, unchanged")

    os.environ["SCHEMALESS_PLUGINS_CACHE__TTL"] = "90"

    config = DynamicConfig(Values, key="plugins").file(str(path)).env("SCHEMALESS_")
    config.set_default("cache.size", 8)
    config.init()

    values = config.current()

    print(f"  cache.ttl   {values['cache.ttl']:<6} ← the environment beats the file")
    print(f"  cache.size  {values['cache.size']:<6} ← the defaults layer, below both")
    print(f"  source_of   {config.source_of('cache.ttl')}")

    del os.environ["SCHEMALESS_PLUGINS_CACHE__TTL"]


def what_it_cannot_claim(path: Path, directory: Path) -> None:
    """The two answers a configuration with no declaration cannot give."""
    show("what it does not pretend to know")

    report = DynamicConfig(Values, key="plugins").file(str(path)).check()

    print(f"  unknown keys found    {len(report.unknown)}")
    print(f"  unknown_checked       {report.unknown_checked}")
    print("\n  An empty list from a configuration with no field list would")
    print("  read as an all-clear it never earned, so it says which it is:")
    print("\n".join(f"  {line}" for line in str(report).splitlines()))

    show("...and a redacting cache is refused rather than faked")

    try:
        (
            DynamicConfig(Values, key="plugins")
            .file(str(path))
            .cache(str(directory / "last.json"), "redacted")
            .init()
        )
    except BackendError as error:
        print(f"  {error}")

    show("secrets=[...] is how a schemaless configuration declares")

    cache = directory / "last.json"
    config = (
        DynamicConfig(Values, key="plugins", secrets=["token"])
        .file(str(path))
        .cache(str(cache), "redacted")
    )
    config.init()

    written = json.loads(cache.read_text())["cached"]

    print(f"  the cache holds  {sorted(k for k in written if not k.startswith('__'))}")
    print(f"  and not          {'token'!r} — the one path secrets= named")
    print(f"  explain('token') {str(config.explain('token')).splitlines()[0]}")


def main() -> None:
    """Runs the three parts end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "plugins.json"
        path.write_text(json.dumps(PLUGINS))

        reading(path)
        the_layers_are_the_same(path)
        what_it_cannot_claim(path, Path(directory))


if __name__ == "__main__":
    main()
