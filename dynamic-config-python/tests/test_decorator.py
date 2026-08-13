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


def test_the_configured_mixin_is_the_form_that_type_checks(workspace: Path) -> None:
    """The decorator alone attaches at runtime, which no checker can see.

    `tests/typing/usage.py` is where the *types* are asserted, under
    `mypy --strict`. This is the runtime half: the mixin's methods
    delegate through the configuration the decorator set, and inheriting
    it changes nothing a user can observe at runtime.
    """
    from dynamic_config import Configured

    Path("config.toml").write_text('[db]\nhost = "mixed-in"\nport = 6543\n')

    @dynamic_config(key="db", files=["config.toml"])
    class Database(Configured, BaseModel):
        host: str = "localhost"
        port: int = 5432

    Database.config.init()

    assert Database.current().host == "mixed-in"
    assert Database.try_current() is not None
    assert Database.source_of("port") is not None
    assert "6543" in str(Database.explain("port"))

    Path("config.toml").write_text('[db]\nhost = "reloaded"\nport = 1\n')
    Database.reload()

    assert Database.current().host == "reloaded"

    # The mixin declares methods, not fields: the model is unchanged.
    assert "config" not in Database.model_fields
    assert "current" not in Database.model_fields


def test_the_mixin_and_the_decorator_agree(workspace: Path) -> None:
    """Both forms read the same configuration the same way."""
    from dynamic_config import Configured

    Path("config.toml").write_text('[db]\nhost = "shared"\n')

    @dynamic_config(key="db", files=["config.toml"], init=True)
    class Plain(BaseModel):
        host: str = "localhost"

    @dynamic_config(key="db", files=["config.toml"], init=True)
    class Mixed(Configured, BaseModel):
        host: str = "localhost"

    assert Plain.current().host == Mixed.current().host == "shared"


def test_reading_before_decoration_says_which_class(workspace: Path) -> None:
    from dynamic_config import Configured

    class Undecorated(Configured, BaseModel):
        host: str = "localhost"

    with pytest.raises(AttributeError, match="config"):
        Undecorated.current()
