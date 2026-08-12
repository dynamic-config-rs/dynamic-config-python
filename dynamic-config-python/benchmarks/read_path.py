"""What a read costs, next to the things it is claimed to cost like.

    python benchmarks/read_path.py

Deliberately not a CI gate and deliberately not `pytest-benchmark`: a
shared runner cannot resolve the difference between an attribute lookup
and an attribute lookup, and a number nobody can reproduce is worse than
no number. This prints the three figures a reader wants — the cached
read, the module-global it is supposed to feel like, and what a
re-validating read would have cost instead — and lets them run it.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path

from pydantic import BaseModel

import dynamic_config
from dynamic_config import DynamicConfig

ROUNDS = 200_000


class Database(BaseModel):
    host: str
    port: int
    pool_size: int = 8


def time_it(label: str, work) -> float:
    for _ in range(1000):
        work()

    started = time.perf_counter()
    for _ in range(ROUNDS):
        work()
    per_call = (time.perf_counter() - started) / ROUNDS * 1e9

    print(f"{label:<34} {per_call:>8.1f} ns/read")

    return per_call


def cpu_model() -> str:
    """The processor's own name for itself, where the platform offers one.

    `platform.processor()` answers "x86_64" on Linux, which says nothing
    about speed; /proc/cpuinfo has the model name, and macOS keeps it in
    sysctl. A number without the machine it was measured on is not a
    measurement, so this is worth the twenty lines.
    """
    try:
        if sys.platform == "linux":
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            import subprocess

            return subprocess.run(
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
    except Exception:  # pragma: no cover - a machine that will not say
        pass

    return platform.processor() or platform.machine() or "unknown"


def memory() -> str:
    """Total RAM, where the platform will say."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")

        return f"{pages * size / 1024**3:.0f} GiB"
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return "unknown"


def environment() -> str:
    """Where these numbers came from, printed with them.

    A benchmark result travels — into a changelog, into a book page, into
    somebody's decision. Without the machine, the interpreter and the
    build it ran against, it is a number pretending to be a fact.
    """
    build = "debug" if sys.flags.dev_mode else "release"

    return "\n".join(
        [
            f"  cpu         {cpu_model()}",
            f"  cores       {os.cpu_count()}",
            f"  memory      {memory()}",
            f"  os          {platform.system()} {platform.release()}",
            (
                f"  python      {platform.python_implementation()} "
                f"{platform.python_version()} ({build})"
            ),
            f"  package     dynamic-config-py {dynamic_config.__version__}",
            f"  engine      dynamic-config {dynamic_config.__engine_version__}",
            f"  rounds      {ROUNDS} per measurement",
        ]
    )


def main() -> None:
    Path("bench-config.toml").write_text(
        '[db]\nhost = "localhost"\nport = 5432\npool_size = 16\n'
    )

    config = DynamicConfig(Database, key="db").file("bench-config.toml")
    config.init()

    payload = {"host": "localhost", "port": 5432, "pool_size": 16}
    module_global = config.current()

    print("measured on\n")
    print(environment())
    print()

    cached = time_it("config.current()", config.current)
    plain = time_it("a module global", lambda: module_global)
    revalidating = time_it(
        "Model.model_validate(dict)", lambda: Database.model_validate(payload)
    )

    print(f"\ncurrent() / global:            {cached / plain:>8.1f}x")
    print(f"validating / current():        {revalidating / cached:>8.1f}x")
    print(
        "\nThe second ratio is the one that matters: it is what every read\n"
        "would cost if the model were validated per read instead of per\n"
        "reload. Ratios travel between machines; the nanoseconds above do\n"
        "not — quote them with the block at the top."
    )

    Path("bench-config.toml").unlink()


if __name__ == "__main__":
    main()
