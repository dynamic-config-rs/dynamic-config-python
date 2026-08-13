"""Fixtures for the binding suite.

Every test gets its own directory and its own configuration object: the
engine keys watchers per instance, so tests that share nothing can run
side by side.

Both fixtures come from `dynamic_config.pytest`, the plugin the package
ships — pytest loads it through its `pytest11` entry point, so there is
no import here to keep in step. That is deliberate: the fixture a user
gets is the fixture two hundred tests in this suite run on, and a
regression in it fails here rather than in their repository.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def workspace(dynamic_config_workspace: Path) -> Path:
    """A scratch directory that is also the working directory."""
    return dynamic_config_workspace


@pytest.fixture(autouse=True)
def _clean_environment(dynamic_config_env: Callable[..., None]) -> None:
    """No test inherits another's variables."""
    dynamic_config_env("DCTEST_", "APP_")
