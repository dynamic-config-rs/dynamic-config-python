"""The last known good, in all three modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import DynamicConfig, InvalidError, ParseError


class Database(BaseModel):
    host: str
    port: int = 5432


def break_the_source() -> None:
    Path("config.toml").write_text("[db\nthis is not toml")


def test_a_full_cache_recovers_everything(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "planted-host"\nport = 7\n')

    first = (
        DynamicConfig(Database, key="db").file("config.toml").cache("last.json", "full")
    )
    first.init()

    break_the_source()

    second = (
        DynamicConfig(Database, key="db").file("config.toml").cache("last.json", "full")
    )
    second.init()

    assert second.current().host == "planted-host"
    assert second.current().port == 7


def test_a_redacted_cache_recovers_what_is_not_secret(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "planted-host"\nport = 7\n')

    first = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("last.json", "redacted")
    )
    first.init()

    assert "planted-host" in Path("last.json").read_text()

    break_the_source()

    second = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("last.json", "redacted")
    )
    second.init()

    assert second.current().host == "planted-host"


def test_a_fingerprint_cache_never_recovers(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "planted-host"\n')

    first = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("last.json", "fingerprint")
    )
    first.init()

    written = json.loads(Path("last.json").read_text())
    assert "planted-host" not in json.dumps(written), "a fingerprint holds no values"

    break_the_source()

    second = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("last.json", "fingerprint")
    )

    with pytest.raises(ParseError):
        second.init()

    assert second.try_current() is None, "diagnose, and still refuse to start"


def test_a_cache_that_no_longer_validates_does_not_resurrect(workspace: Path) -> None:
    class AtLeastOne(BaseModel):
        port: int

    Path("config.toml").write_text("[db]\nport = 5\n")

    first = (
        DynamicConfig(AtLeastOne, key="db")
        .file("config.toml")
        .cache("last.json", "full")
    )
    first.init()

    # The cache holds 5; a stricter model refuses it, and recovery must
    # refuse with it rather than install something the schema rejects.
    class Stricter(BaseModel):
        port: int
        required_now: str

    break_the_source()
    second = (
        DynamicConfig(Stricter, key="db").file("config.toml").cache("last.json", "full")
    )

    with pytest.raises(InvalidError):
        second.init()

    assert second.try_current() is None


def test_an_unknown_cache_mode_is_refused_at_the_door(workspace: Path) -> None:
    with pytest.raises(ValueError, match="fingerprint") as failure:
        DynamicConfig(Database, key="db").cache("last.json", "sometimes")

    assert "fingerprint" in str(failure.value), "the message lists the real modes"
