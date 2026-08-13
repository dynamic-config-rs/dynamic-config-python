"""The base wheel with the remote wheel absent, and the name it keeps free.

`dynamic_config.remote` is a module in *this* wheel that re-exports a
distribution which may not be installed. Two things have to hold, and neither
can be checked in the process running the suite — this checkout has the
remote wheel installed, and even where it does not, an `import` earlier in
the session would have poisoned `sys.modules`:

- **The base wheel imports and behaves identically with the remote wheel
  absent.** It is the ordinary install, and it must not have acquired a
  dependency on the other one.
- **`dynamic_config.remote` raises an ImportError naming the extra.** Not
  `ModuleNotFoundError: No module named 'dynamic_config_remote'`, which tells
  a user nothing about what to install.

So both run in a subprocess with a meta-path finder that hides
`dynamic_config_remote` — which is exactly what a machine without the extra
looks like, and is a fixture rather than an uninstall.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

#: Hides one module from a fresh interpreter, whatever is installed.
#:
#: A `sys.meta_path` finder rather than a `sys.modules[name] = None`: the
#: latter raises `ImportError` from a different place, so a test using it
#: would pass against an implementation that only handled that shape.
WITHOUT_THE_EXTRA = textwrap.dedent(
    """
    import sys


    class Hidden:
        def find_spec(self, name, path=None, target=None):
            if name == "dynamic_config_remote" or name.startswith(
                "dynamic_config_remote."
            ):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)

            return None


    sys.meta_path.insert(0, Hidden())
    """
)


def run(script: str) -> str:
    """Runs a script in an interpreter with the extra hidden.

    Each half is dedented on its own: concatenating them first and dedenting
    the result would find a common prefix of zero and leave the second half
    indented, which is an `IndentationError` in a subprocess rather than a
    test failure that says anything.
    """
    finished = subprocess.run(
        [sys.executable, "-c", WITHOUT_THE_EXTRA + textwrap.dedent(script)],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )

    return finished.stdout.strip()


def test_the_base_wheel_imports_with_the_remote_wheel_absent() -> None:
    printed = run(
        """
        import json
        import tempfile
        from dataclasses import dataclass
        from pathlib import Path

        import dynamic_config
        from dynamic_config import DynamicConfig

        @dataclass
        class Database:
            host: str = "localhost"
            port: int = 5432

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"db": {"host": "here", "port": 1}}))

            config = DynamicConfig(Database, key="db").file(str(path))
            model = config.init_and_current()

        print(model.host, model.port)
        print(sorted(dynamic_config.__all__) == list(dynamic_config.__all__))
        """
    )

    # Not merely importable: a whole load still works, which is what "the
    # base install is unchanged" has to mean.
    assert printed == "here 1\nTrue"


def test_importing_the_remote_module_without_the_extra_names_the_extra() -> None:
    printed = run(
        """
        try:
            import dynamic_config.remote
        except ImportError as absent:
            print(type(absent).__name__)
            print(absent)
        """
    )

    kind, message = printed.split("\n", 1)

    assert kind == "ImportError"
    # The instruction, verbatim — a user who reads only this line can act on
    # it. `ModuleNotFoundError: No module named 'dynamic_config_remote'` is
    # what they would get without this module, and it names nothing they can
    # type.
    assert "pip install dynamic-config-py[remote]" in message
    # And it says what they *can* do with the wheel they already have, which
    # for a store nobody has written a Rust client for is the answer anyway.
    assert "RemoteSource" in message


def test_the_absent_remote_module_does_not_break_a_python_remote_source() -> None:
    # The store written in Python is in this wheel and owes the other one
    # nothing; a user without the extra keeps the whole of item 05.
    printed = run(
        """
        import json
        from dataclasses import dataclass

        from dynamic_config import DynamicConfig, Format, RemoteSource

        @dataclass
        class Database:
            host: str = "localhost"
            port: int = 5432

        class OurService(RemoteSource):
            def fetch(self):
                return json.dumps({"db": {"host": "from-python"}}), Format.JSON

            def describe(self):
                return "our service"

        config = DynamicConfig(Database, key="db").remote(OurService())
        config.refresh_remote()
        config.init()

        print(config.current().host)
        """
    )

    assert printed == "from-python"


def test_the_remote_module_re_exports_the_wheel_when_it_is_installed() -> None:
    # The other side of the same door, run here rather than in the remote
    # package's own suite because this is the module that has to keep the
    # two lists in step.
    import importlib.util

    if importlib.util.find_spec("dynamic_config_remote") is None:
        import pytest

        pytest.skip("the remote wheel is not installed in this environment")

    import dynamic_config_remote

    import dynamic_config.remote as through_the_base_package

    # Set equality, not a subset. This test used to walk only this module's
    # `__all__` and check each name existed over there — which is satisfied
    # by re-exporting *fewer* names than the remote wheel has. So when that
    # wheel gained Consul, Firestore, NATS, Redis and S3, this passed while
    # `from dynamic_config.remote import Redis` was an ImportError. The
    # direction that catches a base wheel falling behind is the missing one.
    assert set(through_the_base_package.__all__) == set(dynamic_config_remote.__all__)

    # And each name is the same object, not merely present under both.
    for name in through_the_base_package.__all__:
        assert getattr(through_the_base_package, name) is getattr(
            dynamic_config_remote, name
        ), name
