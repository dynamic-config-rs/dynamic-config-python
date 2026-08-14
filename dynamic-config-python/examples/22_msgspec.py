"""A `msgspec.Struct` as the schema — the fastest declaration on offer.

    pip install dynamic-config-py[msgspec]
    python examples/22_msgspec.py

msgspec builds an instance from a mapping in C, which is exactly the work
a reload asks of a schema: one resolved mapping, one instance, once. The
declaration reads like a dataclass and validates like Pydantic, and
everything around it is the object you already have — the same sources,
the same precedence, the same watcher, the same diagnostics, the same
last-known-good cache.

Three answers are msgspec's own rather than this library's, and each one
is printed below rather than described:

- **a secret is declared with `Meta(extra=...)`**, because msgspec has no
  `SecretStr` and its `Meta` has a door meant for exactly this;
- **unknown keys are the struct's business** — ignored by default,
  refused if it says `forbid_unknown_fields=True`;
- **`InvalidError.errors` is empty**, because msgspec raises a message
  rather than a report, and inventing one would be inventing structure
  the library never promised.
"""

from __future__ import annotations

import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Annotated

import msgspec

from dynamic_config import DynamicConfig, InvalidError, changed_paths, secret_paths


def show(title: str) -> None:
    """The narration helper, repeated here for the same reason 17 does.

    `_shared` declares its model with Pydantic, and an example about a
    different schema library should not need it installed.
    """
    print(f"\n{title}\n{'─' * len(title)}")


class Level(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"


class Pool(msgspec.Struct):
    max_size: int = 8
    timeout_seconds: float = 5.0


class Database(msgspec.Struct):
    host: str = "localhost"
    port: int = 5432
    level: Level = Level.INFO
    # msgspec has no secret type. `Meta` carries an `extra` mapping for
    # another library's flag, which is the door rather than an invention
    # — and it reads like the dataclass spelling on purpose.
    password: Annotated[str, msgspec.Meta(extra={"secret": True})] = ""
    # A constraint, to show that the two live side by side: `Meta` is
    # msgspec's own vocabulary and the `extra` above is a guest in it.
    workers: Annotated[int, msgspec.Meta(ge=1, le=64)] = 4
    pool: Pool = msgspec.field(default_factory=Pool)


class Strict(msgspec.Struct, forbid_unknown_fields=True):
    """The same declaration, with the other answer to unknown keys."""

    host: str = "localhost"


CONFIG = """
[db]
host = "db.internal"
port = 6543
level = "debug"
password = "hunter2"
workers = 16

[db.pool]
max_size = 32
"""


def main() -> None:
    """Runs the msgspec example end to end."""
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
        db = config.init_and_current()
        print(f"  {db.host}:{db.port}  pool {db.pool.max_size}")

        show("the types a struct declares are the types you get")
        print(f"  level    {db.level!r}   (an Enum member, from its value)")
        print(f"  workers  {db.workers!r}          (inside the declared range)")
        print(f"  pool     {db.pool!r}   (built, not left as a dict)")

        show("the environment arrives as text, and lands as an int")
        # msgspec decodes with `strict=False` here, which is what a
        # configuration needs: every environment variable is a string,
        # and refusing `APP_DB_PORT=7000` for being one is not a mistake
        # anybody made.
        os.environ["APP_DB_PORT"] = "7000"
        config.reload()
        print(f"  port     {config.current().port!r}     (a str in the environment)")
        del os.environ["APP_DB_PORT"]
        config.reload()

        show("secrets are declared in Meta(extra=...), and redacted everywhere")
        print(f"  secret_paths  {secret_paths(Database)}")
        print(f"  the program   {db.password!r}")
        print("  " + str(config.explain("password")).replace("\n", "\n  ").strip())
        print(f"  on disk       {'hunter2' in cache.read_text()}")

        show("a wrong value is refused, and the message carries no value")
        path.write_text(CONFIG.replace("workers = 16", "workers = 999"))

        try:
            config.reload()
        except InvalidError as failure:
            print(f"  refused: {failure}")
            print(f"  errors:  {failure.errors}   (msgspec raises no report)")

        show("...including the message msgspec would have quoted it in")
        path.write_text(CONFIG.replace('level = "debug"', 'level = "verbose"'))

        try:
            config.reload()
        except InvalidError as failure:
            print(f"  refused: {failure}")
            print("  ('verbose' was msgspec's word; the path is this library's)")

        print(f"  still serving workers={config.current().workers}")

        show("unknown keys are the declaration's business")
        stray = Path(directory) / "stray.toml"
        stray.write_text('[db]\nhost = "db.internal"\nstray = 1\n')

        lenient = DynamicConfig(Database, key="db").file(str(stray))
        print(f"  ignored by Database:  host is {lenient.init_and_current().host}")

        strict = DynamicConfig(Strict, key="db").file(str(stray))

        try:
            strict.init()
        except InvalidError as failure:
            print(f"  refused by forbid_unknown_fields: {failure}")

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
