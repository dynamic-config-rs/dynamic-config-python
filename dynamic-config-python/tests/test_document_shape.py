"""What the loader does with a document's shape, and with keys on one side.

The Python half of `dynamic-config/tests/document_shape.rs`, and not a
copy of it: the questions are the same, the answers for "a key the model
does not declare" are **not**. Pydantic ignores an extra by default and
refuses it under ``extra="forbid"``; a dataclass refuses it always,
because a dataclass has no vocabulary for one. That difference is worth a
test each rather than a sentence in a docstring.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Optional

import pytest
from pydantic import BaseModel, ConfigDict

from dynamic_config import DynamicConfig, InvalidError, ParseError


class Server(BaseModel):
    host: str
    port: int


# ── 1. A document with no section header ───────────────────────────────


def test_a_whole_document_needs_no_section_header(workspace: Path) -> None:
    Path("server.json").write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))

    config = DynamicConfig(Server, key="server").whole_document().file("server.json")
    config.init()

    assert config.current().host == "0.0.0.0"
    assert config.current().port == 8000


def test_a_sectioned_load_refuses_a_bare_document_and_says_what_to_do(
    workspace: Path,
) -> None:
    Path("server.json").write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))

    config = DynamicConfig(Server, key="server").file("server.json")

    with pytest.raises(ParseError) as raised:
        config.init()

    assert "is not a table" in str(raised.value)
    assert "whole_document" in str(raised.value), "the refusal names the fix"


def test_the_environment_still_layers_over_a_whole_document(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("server.json").write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))
    monkeypatch.setenv("APP_SERVER_PORT", "9999")

    config = (
        DynamicConfig(Server, key="server")
        .whole_document()
        .file("server.json")
        .env("APP_")
    )
    config.init()

    assert config.current().port == 9999, "the key still names the variable"


def test_an_empty_key_names_nothing_and_the_prefix_stands_alone(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path("server.json").write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))
    monkeypatch.setenv("APP_PORT", "7777")

    config = (
        DynamicConfig(Server, key="").whole_document().file("server.json").env("APP_")
    )
    config.init()

    assert config.current().port == 7777, "no key, no `APP__PORT`"


def test_the_decorator_takes_it_too(workspace: Path) -> None:
    from dynamic_config import dynamic_config

    Path("server.json").write_text(json.dumps({"host": "0.0.0.0", "port": 8000}))

    @dynamic_config(key="server", files=["server.json"], whole_document=True)
    class Decorated(BaseModel):
        host: str
        port: int

    Decorated.config.init()

    assert Decorated.current().port == 8000


# ── 2. A key the file has and the model does not ───────────────────────


def test_pydantic_ignores_a_key_it_does_not_declare(workspace: Path) -> None:
    Path("config.json").write_text(
        json.dumps({"server": {"host": "0.0.0.0", "port": 8000, "hsot": "typo"}})
    )

    config = DynamicConfig(Server, key="server").file("config.json")
    config.init()

    assert config.current().host == "0.0.0.0", "the typo is ignored, not fatal"


def test_pydantic_forbidding_extras_refuses_the_same_file(workspace: Path) -> None:
    class Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")

        host: str

    Path("config.json").write_text(
        json.dumps({"server": {"host": "0.0.0.0", "port": 8000}})
    )

    config = DynamicConfig(Strict, key="server").file("config.json")

    with pytest.raises(InvalidError) as raised:
        config.init()

    assert "port" in str(raised.value)


def test_a_dataclass_refuses_a_key_it_does_not_declare(workspace: Path) -> None:
    """A dataclass refuses what it does not declare.

    It has no `extra` setting, so there is nothing to choose from: the
    binding's own builder refuses the key and names the field it could
    not place.
    """

    @dataclasses.dataclass
    class Plain:
        host: str

    Path("config.json").write_text(
        json.dumps({"server": {"host": "0.0.0.0", "port": 8000}})
    )

    config = DynamicConfig(Plain, key="server").file("config.json")

    with pytest.raises(InvalidError) as raised:
        config.init()

    assert "port" in str(raised.value)
    assert "no such field" in str(raised.value)


# ── 3. One model, two files, half of it in each ────────────────────────


def test_two_files_each_holding_half_the_model(workspace: Path) -> None:
    Path("host.json").write_text(json.dumps({"server": {"host": "0.0.0.0"}}))
    Path("port.json").write_text(json.dumps({"server": {"port": 8000}}))

    config = DynamicConfig(Server, key="server").file("host.json").file("port.json")
    config.init()

    assert config.current().host == "0.0.0.0"
    assert config.current().port == 8000


def test_two_whole_documents_layer_the_same_way(workspace: Path) -> None:
    Path("host.json").write_text(json.dumps({"host": "0.0.0.0"}))
    Path("port.json").write_text(json.dumps({"port": 8000}))

    config = (
        DynamicConfig(Server, key="server")
        .whole_document()
        .file("host.json")
        .file("port.json")
    )
    config.init()

    assert config.current().port == 8000


# ── 4. A field no source supplies ──────────────────────────────────────


def test_a_field_no_source_supplies_fails_the_load_and_names_itself(
    workspace: Path,
) -> None:
    Path("config.json").write_text(json.dumps({"server": {"host": "0.0.0.0"}}))

    config = DynamicConfig(Server, key="server").file("config.json")

    with pytest.raises(InvalidError) as raised:
        config.init()

    assert "port" in str(raised.value), "the error names the field"


def test_a_model_default_covers_what_no_source_supplies(workspace: Path) -> None:
    class Tolerant(BaseModel):
        host: str
        port: int = 8000
        # `Optional[list[str]]`, not `list[str] | None`: Pydantic evaluates a
        # model's annotations when the class is created, and this suite runs
        # on the 3.9 floor, where PEP 604 unions do not evaluate.
        tags: Optional[list[str]] = None

    Path("config.json").write_text(json.dumps({"server": {"host": "0.0.0.0"}}))

    config = DynamicConfig(Tolerant, key="server").file("config.json")
    config.init()

    assert config.current().port == 8000
    assert config.current().tags is None


def test_a_section_no_file_mentions_reads_as_missing_fields(workspace: Path) -> None:
    Path("config.json").write_text(json.dumps({"database": {"url": "postgres://"}}))

    config = DynamicConfig(Server, key="server").file("config.json")

    with pytest.raises(InvalidError) as raised:
        config.init()

    assert "host" in str(raised.value), "an absent section is absent values"
