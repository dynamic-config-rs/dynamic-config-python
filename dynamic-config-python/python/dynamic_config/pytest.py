r"""The pytest plugin: a workspace directory, and an environment nobody shares.

Loaded automatically — the package declares a `pytest11` entry point, so
installing it is all a suite has to do. Nothing here is autouse: a plugin
that arrives with the wheel must not change what a test sees until the
test asks.

    def test_the_service_reads_the_file(dynamic_config_workspace):
        (dynamic_config_workspace / "app.toml").write_text("[db]\nport = 5432\n")
        config = DynamicConfig(Database, key="db").file("app.toml")

        assert config.init_and_current().port == 5432

Two fixtures, because configuration tests fail in two ways that have
nothing to do with the code under test: they write files into whatever
directory pytest happened to start in, and they inherit environment
variables from the developer's shell that the configuration then reads.

A suite that turns entry-point discovery off — CI images increasingly
set `PYTEST_DISABLE_PLUGIN_AUTOLOAD` — asks for this one by name:
`-p dynamic_config.pytest`, or the same string in `addopts`.

This module imports `pytest` and the standard library, and nothing else
— not even the rest of this package. The base install has no
dependencies, and a plugin that quietly required Pydantic would break
`pip install dynamic-config-py` for everyone who runs pytest.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def dynamic_config_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch directory for one test, which is also its working directory.

    Returned as a `Path`, so a test can write files by absolute path, and
    `chdir`-ed into, so a configuration declaring `file("app.toml")` —
    the relative spelling most programs use — finds this test's copy
    rather than the repository's. `monkeypatch` restores the previous
    working directory afterwards.
    """
    monkeypatch.chdir(tmp_path)

    return tmp_path


@pytest.fixture
def dynamic_config_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Removes the environment variables a test's configuration would read.

    A factory, because only the suite knows which names are its own::

        @pytest.fixture(autouse=True)
        def _clean_environment(dynamic_config_env):
            dynamic_config_env("APP_")

    Every variable whose name starts with one of the prefixes is unset
    for the duration of the test and restored by `monkeypatch` after it.
    That is what keeps a developer's shell — or the test that ran before
    this one — from supplying a value the environment layer then outranks
    the file with. Prefixes rather than names, because the environment
    layer works by prefix and a test cannot list what it does not know
    was set.
    """

    def isolate(*prefixes: str) -> None:
        """Unsets every variable whose name starts with one of ``prefixes``."""
        if not prefixes:
            raise ValueError("dynamic_config_env needs at least one prefix")

        for name in list(os.environ):
            if name.startswith(prefixes):
                monkeypatch.delenv(name, raising=False)

    return isolate
