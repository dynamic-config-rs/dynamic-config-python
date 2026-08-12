"""A plain `dataclasses.dataclass` as the schema — no Pydantic anywhere.

    pip install dynamic-config-py        # nothing else
    python examples/17_dataclasses.py

The base install has no dependencies. The engine is compiled into the
wheel, and the stdlib already has a way to declare a record, so a program
that does not want Pydantic in its tree does not get it:

    pip install dynamic-config-py                     dataclasses
    pip install dynamic-config-py[pydantic]           + Pydantic models
    pip install dynamic-config-py[pydantic-settings]  + BaseSettings
    pip install dynamic-config-py[all]                all of it

Everything else is the same object: the same sources, the same
precedence, the same watcher, the same diagnostics, the same
last-known-good cache. What changes is what validation *means*.

A dataclass schema validates **structurally**: required fields present,
no key the class has never heard of, nested dataclasses built
recursively, and each value checked against the type its field declares.
It does not coerce a string into a `datetime`… except where the type
parses its own text (`date`, `UUID`, `Path`, `Decimal` all do). What it
will never do is accept a value of the wrong type quietly — the whole
point of declaring one.
"""

from __future__ import annotations

import datetime
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path

from dynamic_config import DynamicConfig, InvalidError, changed_paths, secret_paths


def show(title: str) -> None:
    """The narration helper the other examples import from `_shared`.

    Repeated here rather than imported, because `_shared` declares its
    model with Pydantic — and an example whose whole claim is "no
    Pydantic anywhere" cannot import a module that needs it.
    """
    print(f"\n{title}\n{'─' * len(title)}")


class Level(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"


@dataclass
class Pool:
    max_size: int = 8
    timeout_seconds: float = 5.0


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432
    level: Level = Level.INFO
    # The stdlib's own extension point, and the natural place for a
    # declaration Pydantic makes with a type: `SecretStr` has no
    # stdlib equivalent, `metadata` does.
    password: str = field(default="", metadata={"secret": True})
    rotated_on: datetime.date = datetime.date(1970, 1, 1)
    ratio: Decimal = Decimal("1")
    pool: Pool = field(default_factory=Pool)


CONFIG = """
[db]
host = "db.internal"
port = 6543
level = "debug"
password = "hunter2"
rotated_on = 2026-01-15
ratio = "1.5"

[db.pool]
max_size = 32
"""


def main() -> None:
    """Runs the dataclass example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.toml"
        path.write_text(CONFIG)
        cache = Path(directory) / "last.json"

        config = (
            DynamicConfig(Database, key="db")
            .file(str(path))
            .env("APP_")
            .cache(str(cache), "redacted")
        )

        show("one line to load and read")
        # `init_and_current` is the two calls that always pair, for the
        # code that wants the values rather than the configuration object.
        db = config.init_and_current()
        print(f"  {db.host}:{db.port}  pool {db.pool.max_size}")

        show("the types a dataclass declares are the types you get")
        print(f"  level       {db.level!r}   (an Enum member, from its value)")
        print(f"  rotated_on  {db.rotated_on!r}   (a date, from TOML's own)")
        print(f"  ratio       {db.ratio!r}   (a Decimal, parsed from text)")
        print(f"  pool        {db.pool!r}   (built, not left as a dict)")

        show("secrets are declared in metadata, and redacted everywhere")
        print(f"  secret_paths  {secret_paths(Database)}")
        print(f"  the program   {db.password!r}")
        print("  " + str(config.explain("password")).replace("\n", "\n  ").strip())
        print(f"  on disk       {'hunter2' in cache.read_text()}")

        show("a wrong type is refused, and the message carries no value")
        path.write_text(CONFIG.replace("port = 6543", 'port = "not-a-number"'))

        try:
            config.reload()
        except InvalidError as failure:
            print(f"  refused: {failure}")

        print(f"  still serving port {config.current().port}")

        show("and a key the class never heard of is refused too")
        path.write_text(
            CONFIG.replace('host = "db.internal"', 'host = "db.internal"\nstray = 1')
        )

        try:
            config.reload()
        except InvalidError as failure:
            print(f"  refused: {failure}")

        show("the diagnostics are the same diagnostics")
        path.write_text(CONFIG)
        previous = config.current()
        config.reload()

        print(f"  source_of('pool.max_size')  {config.source_of('pool.max_size')}")
        print(f"  is_set('level')             {config.is_set('level')}")
        print(f"  check().is_clean            {config.check().is_clean}")
        print(
            "  changed_paths               "
            f"{[str(change) for change in changed_paths(previous, config.current())]}"
        )


if __name__ == "__main__":
    main()
