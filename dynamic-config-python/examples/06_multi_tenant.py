"""One model, one configuration per tenant.

    python examples/06_multi_tenant.py

This is what the instance engine was built for: `DynamicConfig` is a
value, so a process can hold as many as it has tenants, each with its own
sources, its own watcher and its own snapshot.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _shared import Database, show
from dynamic_config import DynamicConfig

TENANTS = {"acme": 8, "umbra": 64, "initech": 200}


def main() -> None:
    """Runs the multi tenant example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        configs: dict[str, DynamicConfig[Database]] = {}

        for tenant, pool_size in TENANTS.items():
            path = root / f"{tenant}.toml"
            path.write_text(
                f'[db]\nhost = "{tenant}.internal"\npool_size = {pool_size}\n'
            )
            config = DynamicConfig(Database, key="db").file(str(path))
            config.init()
            configs[tenant] = config

        show("every tenant, independently configured")
        for tenant, config in configs.items():
            db = config.current()
            print(f"  {tenant:<10} {db.host:<22} pool {db.pool_size}")

        show("reloading one leaves the others alone")
        (root / "acme.toml").write_text(
            '[db]\nhost = "acme.internal"\npool_size = 999\n'
        )
        configs["acme"].reload()

        for tenant, config in configs.items():
            print(f"  {tenant:<10} pool {config.current().pool_size}")


if __name__ == "__main__":
    main()
