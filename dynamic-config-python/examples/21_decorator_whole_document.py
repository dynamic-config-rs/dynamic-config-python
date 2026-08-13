"""The decorator, argument by argument — and a file with no section header.

python examples/21_decorator_whole_document.py

`@dynamic_config(...)` is the declaration-shaped spelling of everything
`DynamicConfig` can be told: every keyword below is one fluent call, and
the class ends up with `config`, `current()`, `try_current()`, `reload()`,
`source_of()` and `explain()` attached.

The one this example is really about is `whole_document=True`: the file a
container image or another tool writes has no reason to carry a section
header, and this is how a decorated model reads one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from _shared import show
from dynamic_config import dynamic_config


def sectioned(directory: Path) -> None:
    """The default: one file, several sections, each model taking one."""
    show("the default — every top-level key is a section")

    path = directory / "config.json"
    path.write_text(
        json.dumps(
            {
                "server": {"host": "127.0.0.1", "port": 8080},
                "db": {"url": "postgres://localhost/app"},
            }
        )
    )

    @dynamic_config(key="server", files=[str(path)], env="APP_", init=True)
    class Server(BaseModel):
        host: str
        port: int

    @dynamic_config(key="db", files=[str(path)], env="APP_", init=True)
    class Database(BaseModel):
        url: str

    print(f"  Server.current()    {Server.current()}")
    print(f"  Database.current()  {Database.current()}")
    print("\n  Two models, one file, neither knowing about the other. That is")
    print("  what the section header buys, and why it is the default.")


def whole_document(directory: Path) -> None:
    """A file that is only this configuration, with nothing above it."""
    show("whole_document=True — the document is the configuration")

    path = directory / "server.json"
    path.write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))

    @dynamic_config(
        key="server",
        files=[str(path)],
        env="APP_",
        whole_document=True,
        init=True,
    )
    class Server(BaseModel):
        host: str
        port: int

    print(f"  Server.current()    {Server.current()}")
    print(f"  source_of('port')   {Server.source_of('port')}")

    show("the key still names everything around the document")

    os.environ["APP_SERVER_PORT"] = "9999"

    @dynamic_config(
        key="server", files=[str(path)], env="APP_", whole_document=True, init=True
    )
    class Reconfigured(BaseModel):
        host: str
        port: int

    print(f"  APP_SERVER_PORT=9999  {Reconfigured.current()}")
    del os.environ["APP_SERVER_PORT"]

    show('...unless there is nothing to call it, and key=""')

    os.environ["NAMELESS_PORT"] = "7777"

    @dynamic_config(
        key="", files=[str(path)], env="NAMELESS_", whole_document=True, init=True
    )
    class Nameless(BaseModel):
        host: str
        port: int

    print(f"  NAMELESS_PORT=7777    {Nameless.current()}")
    print("\n  No key, no APP__PORT: the environment layer is the prefix alone.")
    del os.environ["NAMELESS_PORT"]


def every_argument(directory: Path) -> None:
    """Each keyword, and the fluent call it stands for."""
    show("every argument the decorator takes")

    rows = [
        ("key", "the section, and the name for everything around it"),
        ("files", ".file(path), once each — later files win"),
        ("discover", ".discover(name, paths) — below the listed files"),
        ("env", ".env(prefix) — read above every file"),
        ("nest", '.nest(separator) — "__" unless given'),
        ("allow_empty_env", ".allow_empty_env() — FOO= is a value"),
        ("strict_env", ".strict_env() — refuse off/no/nil"),
        ("whole_document", ".whole_document() — no section header"),
        ("env_files", ".env_file(path) — below the real environment"),
        ("profile_env", ".profile_env(variable) — sibling files"),
        ("cache", ".cache(path, mode) — last known good"),
        ("cache_mode", "redacted (default), full, fingerprint"),
        ("init", "load at decoration time; False by default"),
        ("watch", "start a detached watcher with this debounce"),
    ]

    for name, meaning in rows:
        print(f"  {name:<16} {meaning}")

    print("\n  help(dynamic_config) has the prose for each one, and every")
    print("  method it maps to carries the same list.")

    # The pair worth spelling out: a watcher without a load has nothing to
    # reload, so a class that should be live from its first line asks for
    # both.
    path = directory / "live.json"
    path.write_text(json.dumps({"limits": {"rate": 100}}))

    @dynamic_config(key="limits", files=[str(path)], init=True, watch=0.25)
    class Limits(BaseModel):
        rate: int

    print(f"\n  init=True, watch=0.25 → {Limits.current()}, and following the file")


def main() -> None:
    """Runs the three parts end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)

        sectioned(path)
        whole_document(path)
        every_argument(path)


if __name__ == "__main__":
    main()
