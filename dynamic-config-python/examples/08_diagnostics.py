"""Answering "where does this value come from" without guessing.

python examples/08_diagnostics.py
"""

from __future__ import annotations

import os

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig, changed_paths


def main() -> None:
    """Runs the diagnostics example end to end."""
    with workspace() as path:
        os.environ["DIAG_DB_PORT"] = "7000"

        config = DynamicConfig(Database, key="db").file(str(path)).env("DIAG_")
        config.set_override("pool_size", 128)
        config.init()

        show("source_of — which layer wins, per key")
        for field in ("host", "port", "pool_size"):
            print(f"  {field:<10} {config.source_of(field)}")

        show("is_set — does anything supply it at all")
        print(f"  host        {config.is_set('host')}")
        print(f"  nonexistent {config.is_set('nonexistent')}")

        show("explain — every layer's answer, not just the winner's")
        print(config.explain("pool_size"))

        show("check — would it load, and is anything unknown")
        report = config.check()
        print(f"  clean: {report.is_clean}")
        print(f"  resolved {len(report.resolved)} paths")
        for unknown in report.unknown:
            print(f"  unknown: {unknown.path} (did you mean {unknown.suggestion}?)")

        show("snapshot — the resolved section, as data")
        snapshot = config.snapshot()
        print(f"  keys: {sorted(snapshot.leaf_paths())}")

        show("changed_paths — the audit half of a reload")
        before = config.current()
        after = before.model_copy(update={"pool_size": 256})
        for change in changed_paths(before, after):
            print(f"  {change}")

        del os.environ["DIAG_DB_PORT"]


if __name__ == "__main__":
    main()
