"""The decorator: sugar over the same engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import DynamicConfigError, dynamic_config


def test_the_decorator_attaches_a_configuration(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "decorated"\nport = 1\n')

    @dynamic_config(key="db", files=["config.toml"], env="DCTEST_")
    class Database(BaseModel):
        host: str
        port: int = 5432

    assert Database.try_current() is None, "import time is not load time"

    Database.config.init()

    assert Database.current().host == "decorated"
    assert Database.source_of("port").kind == "file"
    assert "***" not in str(Database.explain("host"))


def test_init_true_loads_at_decoration(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "eager"\n')

    @dynamic_config(key="db", files=["config.toml"], init=True)
    class Database(BaseModel):
        host: str

    assert Database.current().host == "eager"


def test_decorating_twice_is_refused(workspace: Path) -> None:
    Path("config.toml").write_text('[db]\nhost = "h"\n')

    @dynamic_config(key="db", files=["config.toml"])
    class Database(BaseModel):
        host: str

    with pytest.raises(DynamicConfigError) as failure:
        dynamic_config(key="db", files=["config.toml"])(Database)

    assert "already has a configuration" in str(failure.value)


def test_the_decorator_carries_every_source_option(
    workspace: Path, monkeypatch
) -> None:
    (workspace / "etc").mkdir()
    (workspace / "etc" / "found.toml").write_text('[db]\nhost = "discovered"\n')
    Path(".env").write_text("DCTEST_DB_PORT=8\n")
    monkeypatch.setenv("DCTEST_ENV", "prod")
    Path("config.toml").write_text("[db]\nport = 1\n")
    Path("config.prod.toml").write_text("[db]\nport = 2\n")

    @dynamic_config(
        key="db",
        files=["config.toml"],
        discover=("found", ["etc"]),
        env="DCTEST_",
        env_files=[".env"],
        profile_env="DCTEST_ENV",
        cache="last.json",
        init=True,
    )
    class Database(BaseModel):
        host: str
        port: int

    assert Database.current().host == "discovered"
    assert Database.current().port == 8, "the .env layer outranks both files"
    assert Path("last.json").exists()


def test_a_non_model_class_is_refused() -> None:
    from dynamic_config import DynamicConfig

    with pytest.raises(TypeError):
        DynamicConfig(dict, key="db")  # type: ignore[arg-type]


def test_a_field_the_decorator_would_shadow_is_refused(workspace: Path) -> None:
    """Six names go on the class; a field with one of them is a collision."""
    with pytest.raises(DynamicConfigError) as failure:

        @dynamic_config(key="db", files=[])
        class Shadowed(BaseModel):
            reload: str = "not a method"

    assert "reload" in str(failure.value)
    assert "DynamicConfig" in str(failure.value), "the message says what to do instead"
