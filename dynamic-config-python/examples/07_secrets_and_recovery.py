"""Secrets that stay out of the diagnostics, and a cache to start from.

python examples/07_secrets_and_recovery.py
"""

from __future__ import annotations

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig


def main() -> None:
    """Runs the secrets and recovery example end to end."""
    with workspace() as path:
        cache = path.parent / "last-known-good.json"

        config = (
            DynamicConfig(Database, key="db")
            .file(str(path))
            # `redacted` is the mode to reach for: the cache is written on
            # every clean load, and secrets landing on disk should be a
            # decision rather than a side effect.
            .cache(str(cache), "redacted")
        )
        config.init()

        show("the secret is declared by its type, and nothing else")
        print("  password: SecretStr  ← this is the whole declaration")
        print("\n  explain(password):")
        for line in str(config.explain("password")).splitlines():
            print(f"    {line}")

        show("what reached the disk")
        written = cache.read_text()
        print(f"  cache contains 'hunter2'? {'hunter2' in written}")
        print(f"  cache contains the host?  {'db.internal' in written}")

        show("recovery, when the source breaks")
        path.write_text("[db\nthis file is no longer valid TOML")

        recovered = (
            DynamicConfig(Database, key="db")
            .file(str(path))
            .cache(str(cache), "redacted")
        )
        recovered.init()

        db = recovered.current()
        print(f"  started anyway, from the cache: {db.host}, pool {db.pool_size}")
        print("  the secret was not in the cache, so it comes back empty —")
        print("  a live environment would supply it again during recovery")


if __name__ == "__main__":
    main()
