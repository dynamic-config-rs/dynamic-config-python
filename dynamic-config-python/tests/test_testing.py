"""The testing surface: scoped overrides, and the plugin the package ships.

Both are about somebody else's test suite, so the schema here is a plain
`dataclasses.dataclass`: nothing in this file needs Pydantic, and the
plugin in particular must not — the base install has no dependencies and
an auto-loaded plugin that pulled one in would break every pytest run in
an environment that has this package and nothing else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from dynamic_config import DynamicConfig


@dataclass
class Pool:
    max_size: int = 8


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432
    pool_size: int = 4
    pool: Pool = field(default_factory=Pool)


def write(host: str = "from-the-file", port: int = 1) -> None:
    Path("config.toml").write_text(f'[db]\nhost = "{host}"\nport = {port}\n')


def configured() -> DynamicConfig[Database]:
    """A loaded configuration reading the file this module writes."""
    write()
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    return config


# ── overrides() ────────────────────────────────────────────────────────


def test_overrides_pin_for_the_block_and_are_gone_after_it(workspace: Path) -> None:
    config = configured()

    with config.overrides(pool_size=1, host="pinned"):
        assert config.current().pool_size == 1, "reloaded on entry"
        assert config.current().host == "pinned", "an override outranks the file"

    assert config.current().host == "from-the-file", "reloaded again on the way out"
    assert config.current().pool_size == 4


def test_a_doubled_underscore_spells_a_dotted_path(workspace: Path) -> None:
    config = configured()

    with config.overrides(pool__max_size=64):
        assert config.current().pool.max_size == 64

    assert config.current().pool.max_size == 8


def test_nested_blocks_restore_the_layer_they_found(workspace: Path) -> None:
    config = configured()

    with config.overrides(host="outer", pool_size=2):
        with config.overrides(pool_size=99):
            assert config.current().pool_size == 99, "the inner block wins"
            assert config.current().host == "outer", "and the outer one still holds"

        assert config.current().pool_size == 2, (
            "leaving the inner block restores the outer layer rather than clearing it"
        )
        assert config.current().host == "outer"

    assert config.current().pool_size == 4
    assert config.current().host == "from-the-file"


def test_a_block_leaves_an_override_set_before_it_standing(workspace: Path) -> None:
    config = configured()
    config.set_override("host", "set-by-hand")
    config.reload()

    with config.overrides(pool_size=1):
        assert config.current().host == "set-by-hand"

    assert config.current().host == "set-by-hand", (
        "the block restores what it found, and did not own this"
    )


def test_the_restore_happens_on_an_exception(workspace: Path) -> None:
    config = configured()

    def failing_block() -> None:
        with config.overrides(host="pinned"):
            assert config.current().host == "pinned"
            raise RuntimeError("the test failed")

    with pytest.raises(RuntimeError, match="the test failed"):
        failing_block()

    assert config.current().host == "from-the-file", (
        "a failing assertion inside the block must not decide what the next test sees"
    )


def test_an_empty_block_still_restores(workspace: Path) -> None:
    config = configured()

    with config.overrides():
        config.set_override("host", "pinned")
        config.reload()

        assert config.current().host == "pinned"

    assert config.current().host == "from-the-file"


def test_the_block_reloads_once_on_entry_and_once_on_exit(workspace: Path) -> None:
    config = configured()

    with config.overrides(host="pinned"):
        assert config.generation == 2, "entry reloaded once"

    assert config.generation == 3, "and the exit reloaded once more"


# ── The shipped pytest plugin ──────────────────────────────────────────


@pytest.mark.skipif(
    bool(os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD")),
    reason="autoload is off, so the plugin is named by -p rather than discovered",
)
def test_the_plugin_is_loaded_from_its_entry_point(
    pytestconfig: pytest.Config,
) -> None:
    assert pytestconfig.pluginmanager.hasplugin("dynamic_config"), (
        "the pytest11 entry point is how a user's suite gets the fixtures"
    )


def test_the_workspace_fixture_is_the_working_directory(
    dynamic_config_workspace: Path,
) -> None:
    Path("written-here.toml").write_text("[db]\n")

    assert (dynamic_config_workspace / "written-here.toml").exists()
    assert Path.cwd() == dynamic_config_workspace


def test_the_env_fixture_unsets_by_prefix(
    dynamic_config_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCTEST_PLUGIN_HOST", "leaked")
    monkeypatch.setenv("KEPT_PLUGIN_HOST", "mine")

    dynamic_config_env("DCTEST_PLUGIN_")

    assert "DCTEST_PLUGIN_HOST" not in os.environ
    assert os.environ["KEPT_PLUGIN_HOST"] == "mine", "only the named prefixes go"


def test_the_env_fixture_refuses_an_empty_prefix_list(
    dynamic_config_env: Callable[..., None],
) -> None:
    with pytest.raises(ValueError, match="at least one prefix"):
        dynamic_config_env()


# The plugin is auto-loaded, so it is imported in *every* pytest run of
# every environment this package is installed in. If it reached Pydantic
# it would break `pip install dynamic-config-py` for all of them — so the
# import is proved in a subprocess where Pydantic cannot be imported at
# all, rather than merely in one where it happens not to be installed.
_IMPORT_WITH_PYDANTIC_REFUSED = """
import sys


class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name.partition(".")[0] in ("pydantic", "pydantic_settings"):
            raise ImportError(f"{name} is not installed here")

        return None


sys.meta_path.insert(0, Refuse())

import dynamic_config.pytest

assert not [name for name in sys.modules if name.startswith("pydantic")], (
    "the plugin pulled in Pydantic"
)
print("ok")
"""


def test_the_plugin_imports_with_no_pydantic_available() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_WITH_PYDANTIC_REFUSED],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
