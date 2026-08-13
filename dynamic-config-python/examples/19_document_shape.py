"""Four questions about shape, answered by running them.

python examples/19_document_shape.py

1. Must a file be sectioned?  No — ``whole_document()`` reads
   ``{"host": ..., "port": ...}`` with nothing above it.
2. A key the file has and the model does not?  Pydantic ignores it,
   ``extra="forbid"`` refuses it, a dataclass always refuses it.
3. Two files, half the model in each?  One configuration; the later file
   wins where they overlap.
4. A field no source supplies?  The load fails, naming the field —
   unless the model gives it a default.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict

from _shared import show
from dynamic_config import DynamicConfig, DynamicConfigError


class Server(BaseModel):
    """The model these four questions are asked about."""

    host: str
    port: int


def one_document_no_header(directory: Path) -> None:
    """1. The file another tool wrote: no section header, all of it yours."""
    show("1. a document with no section header")

    path = directory / "server.json"
    path.write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))

    # The default reading — every top-level key is a section — cannot make
    # sense of this file, and says so in the terms that fix it.
    try:
        DynamicConfig(Server, key="server").file(str(path)).load()
    except DynamicConfigError as error:
        print(f"  without whole_document(): {str(error).splitlines()[0]}")

    config = DynamicConfig(Server, key="server").whole_document().file(str(path))
    print(f"  with whole_document():    {config.load()}")

    # The key still names everything around the document.
    os.environ["SHAPE_SERVER_PORT"] = "9999"
    config = (
        DynamicConfig(Server, key="server")
        .whole_document()
        .file(str(path))
        .env("SHAPE_")
    )
    print(f"  SHAPE_SERVER_PORT=9999:   {config.load()}")
    del os.environ["SHAPE_SERVER_PORT"]

    # ...and a configuration with nothing to call itself may pass "",
    # whose environment layer is then just the prefix.
    os.environ["NAMELESS_PORT"] = "7777"
    config = (
        DynamicConfig(Server, key="").whole_document().file(str(path)).env("NAMELESS_")
    )
    print(f'  key="" + NAMELESS_PORT:   {config.load()}')
    del os.environ["NAMELESS_PORT"]


def a_key_the_model_does_not_have(directory: Path) -> None:
    """2. The file says more than the model asks for — three answers."""
    show("2. a key the model does not declare")

    path = directory / "extra.json"
    path.write_text(
        json.dumps({"server": {"host": "0.0.0.0", "port": 8000, "owner": "team-a"}})
    )

    # Pydantic's default: ignored. The file may be shared with another
    # model, another tool, or a later version of this program.
    config = DynamicConfig(Server, key="server").file(str(path))
    print(f"  BaseModel (default):  {config.load()}")

    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")

        host: str
        port: int

    try:
        DynamicConfig(Strict, key="server").file(str(path)).load()
    except DynamicConfigError as error:
        print(f"  extra='forbid':       {error}")

    # A dataclass has no `extra` setting, so there is nothing to choose:
    # the binding refuses what the class does not declare, and names it.
    @dataclasses.dataclass
    class Plain:
        host: str
        port: int

    try:
        DynamicConfig(Plain, key="server").file(str(path)).load()
    except DynamicConfigError as error:
        print(f"  dataclass:            {error}")

    # Ignored is not unnoticed: `check` reports the key either way, with a
    # guess when the key is close enough to a field name to be a typo.
    report = DynamicConfig(Server, key="server").file(str(path)).check()
    print(f"  check(): clean={report.is_clean}")
    for unknown in report.unknown:
        suggestion = (
            f", did you mean {unknown.suggestion}?" if unknown.suggestion else ""
        )
        print(f"           unknown key {unknown.path}{suggestion}")


def half_the_model_in_each_file(directory: Path) -> None:
    """3. No single file has to be complete."""
    show("3. one model, two files")

    base = directory / "base.json"
    ports = directory / "ports.json"
    over = directory / "over.json"
    base.write_text(json.dumps({"server": {"host": "0.0.0.0"}}))
    ports.write_text(json.dumps({"server": {"port": 8000}}))
    over.write_text(json.dumps({"server": {"port": 443}}))

    config = DynamicConfig(Server, key="server").file(str(base)).file(str(ports))
    print(f"  half in each:         {config.load()}")

    config = (
        DynamicConfig(Server, key="server")
        .file(str(base))
        .file(str(ports))
        .file(str(over))
    )
    print(f"  the later file wins:  {config.load()}")
    print("\n  Tables merge key by key; arrays are replaced whole, never appended.")
    print("  A file that is not there is skipped, which is what makes an")
    print("  optional secrets file work.")


def a_field_nothing_supplies(directory: Path) -> None:
    """4. The model asks for more than the sources say."""
    show("4. a field no source supplies")

    path = directory / "incomplete.json"
    path.write_text(json.dumps({"server": {"host": "0.0.0.0"}}))

    try:
        DynamicConfig(Server, key="server").file(str(path)).load()
    except DynamicConfigError as error:
        print(f"  required:             {error}")

    class Tolerant(BaseModel):
        host: str
        port: int = 8000
        # `Optional[list[str]]` rather than `list[str] | None`: Pydantic
        # evaluates a model's annotations at class creation, and the floor
        # this package supports is 3.9, where PEP 604 does not evaluate.
        tags: Optional[list[str]] = None

    config = DynamicConfig(Tolerant, key="server").file(str(path))
    print(f"  with a default:       {config.load()}")

    print("\n  A value the program computes — but a file need not state — is")
    print("  set_default, the layer below the files. A section no file")
    print("  mentions is not a separate error: it is these missing fields.")


def main() -> None:
    """Runs the four questions end to end."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)

        one_document_no_header(path)
        a_key_the_model_does_not_have(path)
        half_the_model_in_each_file(path)
        a_field_nothing_supplies(path)


if __name__ == "__main__":
    main()
