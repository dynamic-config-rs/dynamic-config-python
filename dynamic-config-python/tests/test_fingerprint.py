"""`fingerprint()`, and the two properties that make it printable.

The Rust suite's `tests/fingerprint.rs` pins these on that side. Asked
again here because the binding is where a fleet actually reads it: a pod
prints a string, another pod prints a string, and either they match or
one of them is running a configuration nobody meant to ship.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dynamic_config import DynamicConfig, Values

SECRET = "hunter2-planted-secret"


def built(path: Path) -> DynamicConfig[Values]:
    config = DynamicConfig(Values, "svc", secrets=["password"])
    config.file(str(path))
    config.init()

    return config


@pytest.fixture
def written(tmp_path: Path):
    def write(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")

        return path

    return write


def test_the_same_configuration_fingerprints_the_same_in_any_format(written) -> None:
    """Two hosts, one configuration, two formats.

    A digest that disagreed here would report drift between hosts that are
    configured identically and merely chose different formats.
    """
    as_json = written(
        "config.json",
        json.dumps({"svc": {"host": "db.internal", "port": 5432, "password": SECRET}}),
    )
    # Different syntax, different key order, the same configuration.
    as_toml = written(
        "config.toml",
        f'[svc]\nport = 5432\npassword = "{SECRET}"\nhost = "db.internal"\n',
    )

    assert built(as_json).fingerprint() == built(as_toml).fingerprint()


def test_rotating_a_secret_does_not_move_it(written) -> None:
    """The property that makes it printable.

    A digest that changed when a password changed would let anyone holding a
    candidate password confirm it against a logged fingerprint.
    """
    before = written(
        "before.json",
        json.dumps({"svc": {"host": "db.internal", "password": SECRET}}),
    )
    after = written(
        "after.json",
        json.dumps({"svc": {"host": "db.internal", "password": "correct-horse"}}),
    )

    assert built(before).fingerprint() == built(after).fingerprint()


def test_changing_anything_else_moves_it(written) -> None:
    """And the property that makes it useful."""
    before = written(
        "before.json",
        json.dumps({"svc": {"host": "db.internal", "port": 5432, "password": SECRET}}),
    )
    after = written(
        "after.json",
        json.dumps({"svc": {"host": "db.internal", "port": 6432, "password": SECRET}}),
    )

    assert built(before).fingerprint() != built(after).fingerprint()


def test_it_names_its_algorithm_and_carries_no_value(written) -> None:
    path = written(
        "config.json",
        json.dumps({"svc": {"host": "db.internal", "password": SECRET}}),
    )

    fingerprint = built(path).fingerprint()

    assert fingerprint is not None
    digest = fingerprint.removeprefix("sha256:")

    assert digest != fingerprint, "a fingerprint names its algorithm"
    assert len(digest) == 64
    assert SECRET not in fingerprint


def test_there_is_no_fingerprint_before_the_first_load(written) -> None:
    """Absence rather than a digest of nothing.

    The digest of an empty document would compare equal across every process
    that had not started yet.
    """
    path = written("config.json", json.dumps({"svc": {"host": "db.internal"}}))

    config = DynamicConfig(Values, "svc", secrets=["password"])
    config.file(str(path))

    assert config.fingerprint() is None

    config.init()

    assert config.fingerprint() is not None
