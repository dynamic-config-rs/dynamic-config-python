"""What the examples have in common: a model and a config file to read.

Each example is a script you can run — `python examples/<name>.py` — and
none of them needs a server, a network or a setup step. They write their
own configuration into a temporary directory so a checkout stays clean.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr


class Database(BaseModel):
    """The model every example configures."""

    host: str = "localhost"
    port: int = 5432
    pool_size: int = Field(default=8, ge=1, le=1000)
    password: SecretStr = SecretStr("")


CONFIG = """
[db]
host = "db.internal"
pool_size = 16
password = "hunter2"
"""


@contextmanager
def workspace(document: str = CONFIG, name: str = "config.toml") -> Iterator[Path]:
    """A temporary directory holding a configuration file."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / name
        path.write_text(document)

        yield path


def show(title: str) -> None:
    print(f"\n{title}\n{'─' * len(title)}")
