#!/usr/bin/env python3
"""The conformance suite's Python runner — ~50 lines of glue, on purpose.

Reads the engine repository's ``conformance/cases`` (path from
``CONFORMANCE_DIR``, defaulting to the sibling checkout), builds each
case through the public API, and compares ``current()`` to
``expected.json``. A disagreement names the case and is a FINDING —
fix the engine or the binding, never this runner.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

from dynamic_config import DynamicConfig, Values


def plain(value: object) -> object:
    """A ``Values`` tree as builtin dicts/lists/scalars, for comparison."""
    if isinstance(value, Values):
        return {key: plain(value[key]) for key in value.keys()}
    if isinstance(value, dict):
        return {key: plain(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(inner) for inner in value]
    return value


def run_case(case: pathlib.Path) -> str | None:
    args = json.loads((case / "args.json").read_text())
    env = json.loads((case / "env.json").read_text())
    expected = json.loads((case / "expected.json").read_text())

    for key, value in env.items():
        os.environ[key] = value

    try:
        config = DynamicConfig(Values, key=args["key"]).file(str(case / "config.toml"))

        if prefix := args.get("env_prefix"):
            config = config.env(prefix)
        if variable := args.get("profile_env"):
            config = config.profile_env(variable)
        if directory := args.get("secrets_dir"):
            config = config.secrets_dir(str(case / directory))
        for name in args.get("env_files", []):
            config = config.env_file(str(case / name))
        if args.get("whole_document"):
            config = config.whole_document()
        if missing := args.get("extra_missing_file"):
            config = config.file(str(case / missing))

        if defaults := args.get("defaults"):
            config.set_defaults(defaults)
        for path, value in flatten(args.get("set") or {}):
            config.set_assignments([f"{path}={value}"])
        for path, value in flatten_json(args.get("overrides") or {}):
            config.set_override(path, value)
        for old, new in (args.get("aliases") or {}).items():
            config.alias(old, new)

        resolved = plain(config.init_and_current())
    finally:
        for key in env:
            del os.environ[key]

    if resolved != expected:
        return f"resolved {json.dumps(resolved, sort_keys=True)} but expected {json.dumps(expected, sort_keys=True)}"

    return None


def flatten(tree: dict, prefix: str = ""):
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from flatten(value, path)
        else:
            yield path, value


def flatten_json(tree: dict, prefix: str = ""):
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            yield from flatten_json(value, path)
        else:
            yield path, value


def main() -> int:
    root = pathlib.Path(
        os.environ.get("CONFORMANCE_DIR", "../dynamic-config/conformance/cases")
    )

    if not root.is_dir():
        print(f"no cases at {root}; set CONFORMANCE_DIR", file=sys.stderr)
        return 2

    failures = []

    for case in sorted(p for p in root.iterdir() if p.is_dir()):
        if reason := run_case(case):
            failures.append(f"{case.name}: {reason}")

    if failures:
        print("conformance disagreements:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print(f"{len(list(root.iterdir()))} cases agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
