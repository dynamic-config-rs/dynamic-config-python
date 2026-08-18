"""The engine's diagnostics through `logging` — the bridge, from outside.

Until 0.7 the compiled engine wrote its watcher and recovery lines
straight to file descriptor 2: `logging.basicConfig` could not reach
them, `caplog` could not see them, and a structured-logging setup got
plain text interleaved into its stream from a Rust thread. The bridge
forwards them as ordinary records on `dynamic_config.engine`; these
tests are the claims a consumer relies on, each from the outside.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from dynamic_config import DynamicConfig, configure_logging


@dataclass
class Probe:
    n: int = 0


def _write(path: Path, n: int) -> None:
    path.write_text(json.dumps({"probe": {"n": n}}))


def _reload_until(config, generation: int, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while config.generation < generation and time.monotonic() < deadline:
        time.sleep(0.02)


def test_a_reload_arrives_as_a_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.json"
    _write(path, 1)

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    with caplog.at_level(logging.INFO, logger="dynamic_config.engine"):
        with config.watching(debounce=0.05):
            _write(path, 2)
            _reload_until(config, 2)

        deadline = time.monotonic() + 2
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.02)

    assert caplog.records, "nothing crossed the bridge"

    record = caplog.records[0]

    assert record.name == "dynamic_config.engine"
    assert record.levelno == logging.INFO
    assert "reloaded" in record.getMessage()
    assert not record.getMessage().startswith("[dynamic-config]")


def test_a_failed_reload_is_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.json"
    _write(path, 1)

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    with (
        caplog.at_level(logging.WARNING, logger="dynamic_config.engine"),
        config.watching(debounce=0.05),
    ):
        path.write_text("this is not json {")

        deadline = time.monotonic() + 5
        while not caplog.records and time.monotonic() < deadline:
            time.sleep(0.02)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]

    assert warnings, "the refusal never became a warning"
    assert "keeping the previous snapshot" in warnings[0].getMessage()


def test_the_level_knob_silences_the_info_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.json"
    _write(path, 1)

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    configure_logging(level=logging.WARNING)

    try:
        with caplog.at_level(logging.DEBUG, logger="dynamic_config.engine"):
            with config.watching(debounce=0.05):
                _write(path, 2)
                _reload_until(config, 2)

            time.sleep(0.3)

        assert not [r for r in caplog.records if r.levelno == logging.INFO], (
            "WARNING left the INFO reload line audible"
        )
    finally:
        configure_logging(level=logging.INFO)


def test_raw_stderr_restores_the_old_behaviour() -> None:
    """`raw_stderr=True` — and only a subprocess can read fd 2 honestly."""
    script = r"""
import json, sys, tempfile, time
from dataclasses import dataclass
from pathlib import Path

import dynamic_config
from dynamic_config import DynamicConfig

dynamic_config.configure_logging(raw_stderr=True)

@dataclass
class Probe:
    n: int = 0

with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "config.json"
    path.write_text(json.dumps({"probe": {"n": 1}}))

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    with config.watching(debounce=0.05):
        path.write_text(json.dumps({"probe": {"n": 2}}))
        deadline = time.time() + 5
        while config.generation < 2 and time.time() < deadline:
            time.sleep(0.02)

    # Give the line a beat; it is written synchronously by the watcher,
    # but the watcher is not this thread.
    time.sleep(0.2)
"""
    finished = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert finished.returncode == 0, finished.stderr
    assert "[dynamic-config]" in finished.stderr, (
        f"raw_stderr did not restore the prefix: {finished.stderr!r}"
    )


def test_a_slow_hook_holding_the_gil_cannot_deadlock_the_bridge(
    tmp_path: Path,
) -> None:
    """The hazard the design exists for.

    Reload hooks run on the watcher thread *holding the GIL*; the log
    sink runs on that same thread an instant later. A sink that took the
    GIL would deadlock right there. Ours only pushes into a channel — so
    a slow hook plus a storm of reloads must finish, under a hard
    timeout enforced by the suite's own runner.
    """
    path = tmp_path / "config.json"
    _write(path, 1)

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    def slow_hook(previous, current) -> None:
        time.sleep(0.05)

    guard = config.on_reload(slow_hook)

    try:
        for n in range(2, 12):
            _write(path, n)
            config.reload()

        assert config.generation >= 11
    finally:
        guard.close()


def test_a_reload_storm_loses_no_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "config.json"
    _write(path, 1)

    config = DynamicConfig(Probe, key="probe").file(str(path))
    config.init()

    with caplog.at_level(logging.INFO, logger="dynamic_config.engine"):
        with config.watching(debounce=0.02):
            for n in range(2, 22):
                _write(path, n)
                _reload_until(config, n)

        deadline = time.monotonic() + 2
        while len(caplog.records) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)

    lines = [r.getMessage() for r in caplog.records]

    assert all("reloaded" in line for line in lines)

    # Twenty reloads, twenty records: the bounded channel is deep enough
    # for a storm, and nothing was shed on the way to `logging`. (The
    # lines are identical by content — the engine's reload message names
    # the config, not the generation — so ordering is a property of the
    # single forwarder thread rather than something a test can observe.)
    assert len(lines) == 20, f"{len(lines)} of 20 records arrived"
