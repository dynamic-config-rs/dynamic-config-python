"""Files, the environment, profiles and .env — in precedence order.

python examples/02_layering.py
"""

from __future__ import annotations

import os

from _shared import Database, show, workspace
from dynamic_config import DynamicConfig


def main() -> None:
    """Runs the layering example end to end."""
    with workspace() as path:
        directory = path.parent

        # A profile's sibling file, and a .env below the real environment.
        (directory / "config.production.toml").write_text("[db]\npool_size = 64\n")
        (directory / ".env").write_text("LAYERS_DB_PORT=6000\n")
        os.environ["LAYERS_ENV"] = "production"
        os.environ["LAYERS_DB_HOST"] = "from-the-environment"

        config = (
            DynamicConfig(Database, key="db")
            .file(str(path))
            .env("LAYERS_")
            .env_file(str(directory / ".env"))
            .profile_env("LAYERS_ENV")
        )
        config.init()

        db = config.current()

        show("who won what")
        print(f"  host      {db.host:<24} ← the environment beats every file")
        print(f"  port      {db.port:<24} ← the .env file, no real variable set")
        print(f"  pool_size {db.pool_size:<24} ← the production profile's sibling file")

        show("the whole argument for one key")
        print(config.explain("pool_size"))

        for name in ("LAYERS_ENV", "LAYERS_DB_HOST"):
            del os.environ[name]


if __name__ == "__main__":
    main()
