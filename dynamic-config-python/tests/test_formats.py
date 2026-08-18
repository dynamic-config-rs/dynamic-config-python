"""The two flat formats, read through the wheel.

The parsers and their dialects are the engine's and are tested there;
what belongs here is the seam — a `.ini` or `.properties` path handed to
`.file()` resolves, watches and errors exactly like any other format.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from dynamic_config import DynamicConfig, ParseError


@dataclass
class Database:
    host: str = ""
    port: int = 0


def test_an_ini_file_loads_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text("[db]\nhost = db.internal\nport = 5432\n")

    config = DynamicConfig(Database, key="db").file(str(path))
    config.init()

    assert config.current() == Database(host="db.internal", port=5432)

    path.write_text("[db]\nhost = moved\nport = 5433\n")
    config.reload()

    assert config.current().host == "moved"


def test_a_properties_file_nests_its_dotted_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.properties"
    path.write_text("db.host = db.internal\ndb.port = 5432\n")

    config = DynamicConfig(Database, key="db").file(str(path))
    config.init()

    assert config.current() == Database(host="db.internal", port=5432)


def test_a_flat_format_error_names_the_line_and_no_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.ini"
    path.write_text("[db]\npassword hunter2 with no equals\n")

    config = DynamicConfig(Database, key="db").file(str(path))

    with pytest.raises(ParseError) as caught:
        config.init()

    text = str(caught.value)

    assert "line 2" in text
    assert "hunter2" not in text
