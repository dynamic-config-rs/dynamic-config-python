"""Fixtures for the binding suite.

Every test gets its own directory and its own configuration object: the
engine keys watchers per instance, so tests that share nothing can run
side by side.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch directory that is also the working directory."""
    monkeypatch.chdir(tmp_path)

    return tmp_path


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits another's variables."""
    for name in list(os.environ):
        if name.startswith(("DCTEST_", "APP_")):
            monkeypatch.delenv(name, raising=False)
