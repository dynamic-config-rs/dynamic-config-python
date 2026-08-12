"""Interpreter shutdown while the engine is still working.

The classic embedding crash: a background thread calls into Python after
finalization has begun, and the process dies on the way out — with the
work already done, so nobody notices until it happens in production. The
binding's answer is to stop every watcher at ``atexit``, while the
interpreter is still whole; these run it for real, in a subprocess,
because the failure is *the process exiting badly* and cannot be asserted
from inside.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def run(body: str, workspace: Path) -> subprocess.CompletedProcess[str]:
    # Each piece is dedented on its own: the preamble and the body are
    # written at different indents, and one dedent over the concatenation
    # would only strip the shallower of the two.
    script = textwrap.dedent(PREAMBLE) + textwrap.dedent(body)
    (workspace / "script.py").write_text(script)

    return subprocess.run(
        [sys.executable, "script.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )


PREAMBLE = """
    from pathlib import Path
    from pydantic import BaseModel
    from dynamic_config import DynamicConfig

    class Database(BaseModel):
        host: str
        port: int

    def write(port):
        Path("config.toml").write_text(f'[db]\\nhost = "h"\\nport = {port}\\n')

    write(1)
    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
"""


def test_exiting_with_a_detached_watcher_is_clean(workspace: Path) -> None:
    result = run(
        """
        watch = config.watch(debounce=0.05, poll_interval=0.05)
        watch.detach()          # nothing will ever stop it but the exit
        print("running", config.current().port)
        """,
        workspace,
    )

    assert result.returncode == 0, result.stderr
    assert "running 1" in result.stdout
    assert "Fatal" not in result.stderr
    assert "Traceback" not in result.stderr


def test_exiting_mid_reload_storm_is_clean(workspace: Path) -> None:
    result = run(
        """
        import threading, time

        config.watch(debounce=0.01, poll_interval=0.01).detach()

        # A writer thread churning the file while the process exits: the
        # watcher is mid-flight when finalization starts, which is exactly
        # the window that crashes embedders who touch Python from a
        # background thread on the way out.
        def churn():
            port = 2
            while True:
                write(port)
                port += 1
                time.sleep(0.01)

        threading.Thread(target=churn, daemon=True).start()

        # Wait for the watcher to have *actually* reloaded, with a
        # deadline — a fixed sleep would assert the scheduler rather than
        # the code, and a loaded machine would make it lie either way.
        deadline = time.monotonic() + 30
        while config.generation < 2 and time.monotonic() < deadline:
            time.sleep(0.02)

        print("exiting at", config.generation >= 2)
        """,
        workspace,
    )

    assert result.returncode == 0, result.stderr
    assert "exiting at True" in result.stdout, (result.stdout, result.stderr)
    assert "Fatal Python error" not in result.stderr


def test_a_hook_holding_the_configuration_does_not_wedge_the_exit(
    workspace: Path,
) -> None:
    result = run(
        """
        import time

        # A hook that reads back through the configuration it belongs to:
        # a reference cycle through Rust, which must still collect.
        config.on_reload(lambda old, new: config.current())
        config.watch(debounce=0.01, poll_interval=0.01).detach()

        for port in range(2, 8):
            write(port)
            time.sleep(0.05)

        print("survived", config.try_current() is not None)
        """,
        workspace,
    )

    assert result.returncode == 0, result.stderr
    assert "survived True" in result.stdout


def test_dropping_the_configuration_stops_its_watcher(workspace: Path) -> None:
    result = run(
        """
        import gc

        watch = config.watch(debounce=0.05, poll_interval=0.05)
        del config, watch
        gc.collect()
        print("collected")
        """,
        workspace,
    )

    assert result.returncode == 0, result.stderr
    assert "collected" in result.stdout
