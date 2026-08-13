"""What a user writes, checked the way a user's CI checks it.

`mypy --strict` runs over this file in the gate. It is not a pytest
module: nothing here is executed, and its whole job is to fail the build
when the *types* a caller sees regress — which is invisible to a test
suite, because `Database.current().host` runs perfectly well while a
checker calls it an error and an editor offers no completion.

That is exactly what happened to the decorator: six members attached at
runtime, `attr-defined` errors for anyone type-checking, and a green
suite the whole time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

from dynamic_config import (
    Configured,
    DynamicConfig,
    Explanation,
    Format,
    Origin,
    RemoteSource,
    dynamic_config,
)


class Database(BaseModel):
    host: str = "localhost"
    port: int = 5432


@dataclass
class Cache:
    url: str = "redis://localhost"
    ttl: int = 60


# ── The class API keeps the caller's type ──────────────────────────────

config = DynamicConfig(Database, key="db").file("config.toml").env("APP_")
model: Database = config.init_and_current()
host: str = model.host
maybe: Optional[Database] = config.try_current()
origin: Optional[Origin] = config.source_of("port")
explanation: Explanation = config.explain("port")
generation: int = config.generation


# ── A dataclass schema, same surface ───────────────────────────────────

cached = DynamicConfig(Cache, key="cache").file("config.toml")
url: str = cached.init_and_current().url


# ── The decorator, with `Configured`: the typed form ───────────────────


@dynamic_config(key="db", files=["config.toml"])
class Typed(Configured, BaseModel):
    host: str = "localhost"
    port: int = 5432


Typed.config.init()
typed_host: str = Typed.current().host
typed_maybe: Optional[Typed] = Typed.try_current()
Typed.reload()
typed_origin: Optional[Origin] = Typed.source_of("port")
typed_explanation: Explanation = Typed.explain("port")


# ── Hooks and guards ───────────────────────────────────────────────────


def resize(previous: Optional[Database], current: Database) -> None:
    del previous, current


guard = config.on_reload(resize)
guard.close()

filtered = config.on_change("port")(resize)
filtered.close()


# ── A remote store written in Python ───────────────────────────────────


class OurService(RemoteSource):
    def fetch(self) -> tuple[str, Format]:
        return '{"db": {"port": 5432}}', Format.JSON

    def describe(self) -> str:
        return "our service"


remote_config: DynamicConfig[Database] = DynamicConfig(Database, key="db").remote(
    OurService()
)
remote_config.refresh_remote()
remote_config.clear_remote()
store: Optional[str] = remote_config.remote_description
