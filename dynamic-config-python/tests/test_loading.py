"""Layering, precedence and the source surface — the Rust suite, ported."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import BackendError, DynamicConfig, EnvError, InvalidError


class Database(BaseModel):
    host: str
    port: int = 5432
    tls: bool = False


def test_later_files_win_and_tables_merge(workspace: Path) -> None:
    Path("base.toml").write_text('[db]\nhost = "base"\nport = 1\n')
    Path("over.toml").write_text("[db]\nport = 2\n")

    config = DynamicConfig(Database, key="db").file("base.toml").file("over.toml")
    config.init()

    assert config.current().host == "base", "the untouched key survives the merge"
    assert config.current().port == 2, "the later file wins"


def test_a_missing_file_is_skipped(workspace: Path) -> None:
    Path("present.toml").write_text('[db]\nhost = "here"\n')

    config = DynamicConfig(Database, key="db").file("present.toml").file("absent.toml")
    config.init()

    assert config.current().host == "here"


def test_the_environment_beats_every_file(workspace: Path, monkeypatch) -> None:
    Path("config.toml").write_text('[db]\nhost = "file"\nport = 1\n')
    monkeypatch.setenv("DCTEST_DB_PORT", "9999")

    config = DynamicConfig(Database, key="db").file("config.toml").env("DCTEST_")
    config.init()

    assert config.current().port == 9999
    assert config.current().host == "file"


def test_nested_variables_use_the_doubled_separator(
    workspace: Path, monkeypatch
) -> None:
    class Pool(BaseModel):
        max_size: int

    class Server(BaseModel):
        pool: Pool

    monkeypatch.setenv("DCTEST_SRV_POOL__MAX_SIZE", "64")

    config = DynamicConfig(Server, key="srv").env("DCTEST_")
    config.init()

    assert config.current().pool.max_size == 64


def test_a_required_value_nobody_supplies_is_a_missing_error(workspace: Path) -> None:
    Path("config.toml").write_text("[db]\nport = 1\n")

    config = DynamicConfig(Database, key="db").file("config.toml")

    # Pydantic owns the schema, so a missing required field is *its*
    # refusal — reported as an invalid configuration, with the paths.
    with pytest.raises(InvalidError) as failure:
        config.init()

    assert "host" in str(failure.value)
    assert config.try_current() is None


def test_a_value_of_the_wrong_type_names_the_field(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\nport = "not-a-number"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")

    with pytest.raises(InvalidError) as failure:
        config.init()

    assert "port" in str(failure.value)


def test_strict_env_refuses_an_ambiguous_spelling(workspace: Path, monkeypatch) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\n')
    monkeypatch.setenv("DCTEST_DB_TLS", "off")

    loose = DynamicConfig(Database, key="db").file("config.toml").env("DCTEST_")
    loose.init()
    assert loose.current().tls is False, "loose parsing reads `off` as a string"

    strict = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .env("DCTEST_")
        .strict_env()
    )

    with pytest.raises(EnvError) as failure:
        strict.init()

    assert "DCTEST_DB_TLS" in str(failure.value), "the refusal names the variable"


def test_env_files_sit_below_the_real_environment(workspace: Path, monkeypatch) -> None:
    Path("config.toml").write_text('[db]\nhost = "file"\n')
    Path(".env").write_text("DCTEST_DB_PORT=111\n")
    monkeypatch.setenv("DCTEST_DB_PORT", "222")

    config = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .env("DCTEST_")
        .env_file(".env")
    )
    config.init()

    assert config.current().port == 222, "an exported variable beats a repository file"


def test_profiles_overlay_a_sibling_file(workspace: Path, monkeypatch) -> None:
    Path("config.toml").write_text('[db]\nhost = "base"\nport = 1\n')
    Path("config.production.toml").write_text("[db]\nport = 2\n")
    monkeypatch.setenv("DCTEST_ENV", "production")

    config = (
        DynamicConfig(Database, key="db").file("config.toml").profile_env("DCTEST_ENV")
    )
    config.init()

    assert config.current().port == 2
    assert config.current().host == "base"


def test_discovery_sits_below_listed_files(workspace: Path) -> None:
    (workspace / "etc").mkdir()
    (workspace / "etc" / "config.toml").write_text(
        '[db]\nhost = "discovered"\nport = 1\n'
    )
    Path("explicit.toml").write_text("[db]\nport = 2\n")

    config = (
        DynamicConfig(Database, key="db")
        .discover("config", ["etc"])
        .file("explicit.toml")
    )
    config.init()

    assert config.current().host == "discovered"
    assert config.current().port == 2, "a listed file outranks a discovered one"


def test_runtime_layers_bracket_the_rest(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "file"\nport = 1\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.set_default("tls", True)
    config.set_override("port", 7)
    config.init()

    assert config.current().tls is True, "a default fills what no file states"
    assert config.current().port == 7, "an override outranks the file"

    config.clear_overrides()
    config.reload()
    assert config.current().port == 1, "and clearing it gives the file back"


def test_aliases_keep_old_spellings_working(workspace: Path) -> None:
    class Pool(BaseModel):
        max_size: int = 8

    class Server(BaseModel):
        pool: Pool = Pool()

    Path("config.toml").write_text("[srv.pool]\nsize = 64\n")

    config = DynamicConfig(Server, key="srv").file("config.toml")
    config.alias("pool.size", "pool.max_size")
    config.init()

    assert config.current().pool.max_size == 64


def test_bind_env_maps_one_field_to_one_variable(workspace: Path, monkeypatch) -> None:
    Path("config.toml").write_text('[db]\nhost = "file"\n')
    monkeypatch.setenv("DCTEST_PORT_PLAIN", "4242")

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.bind_env("port", "DCTEST_PORT_PLAIN")
    config.init()

    assert config.current().port == 4242


def test_load_validates_without_installing(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "candidate"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    candidate = config.load()

    assert candidate.host == "candidate"
    assert config.try_current() is None, "load installs nothing"

    config.init()
    assert config.current().host == "candidate"


def test_sources_cannot_change_after_the_first_load(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    with pytest.raises(BackendError) as failure:
        config.file("late.toml")

    assert "after the first load" in str(failure.value)
