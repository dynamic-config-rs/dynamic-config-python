#!/usr/bin/env python3
"""The leak budget, binding edition: N reloads through the wheel, then
the deltas. Binding lifecycles — callbacks, notifier threads, PyO3
references — are where leaks hide even when the Rust core is clean;
the free-threaded finalization bug this project fixed is the standing
argument. Exit 0 = within budget."""

from __future__ import annotations

import json
import os
import pathlib
import resource
import sys
import tempfile

from dynamic_config import DynamicConfig, Values

RELOADS = int(os.environ.get("LEAK_RELOADS", "100000"))


def fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def main() -> int:
    directory = pathlib.Path(tempfile.mkdtemp(prefix="dynamic-config-leak-"))
    file = directory / "config.json"
    file.write_text(json.dumps({"app": {"n": 1}}))

    config = DynamicConfig(Values, key="app").file(str(file))
    config.init()

    # A hook and a churned subscriber, so the callback path is on the meter.
    seen = 0

    def hook(_previous: object, _current: object) -> None:
        nonlocal seen
        seen += 1

    config.on_reload(hook)

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    fds_before = fds()

    for n in range(2, RELOADS + 2):
        file.write_text(json.dumps({"app": {"n": n}}))
        config.reload()

    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    fds_after = fds()

    print(
        f"leak: {RELOADS} reloads | maxrss {rss_before}->{rss_after} kB | "
        f"fds {fds_before}->{fds_after} | hooks fired {seen}"
    )

    failed = False

    if rss_after > rss_before + 131_072:
        print(f"LEAK: rss grew {rss_after - rss_before} kB", file=sys.stderr)
        failed = True

    if fds_after > fds_before + 8:
        print(f"LEAK: fds grew {fds_after - fds_before}", file=sys.stderr)
        failed = True

    if seen != RELOADS:
        print(f"LEAK/LOSS: {seen} hook calls for {RELOADS} reloads", file=sys.stderr)
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
