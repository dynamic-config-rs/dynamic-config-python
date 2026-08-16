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

import msgspec
from pydantic import BaseModel
from typing_extensions import assert_type

from dynamic_config import (
    AsyncRemoteSource,
    Backpressure,
    ConfigGroup,
    ConfigStatus,
    Configured,
    Dispatch,
    DynamicConfig,
    Explanation,
    Format,
    HookGuard,
    Origin,
    RemoteSource,
    Snapshot,
    configure_executor,
    dynamic_config,
    executor,
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


# ── A msgspec Struct, same surface ─────────────────────────────────────


class Queue(msgspec.Struct):
    url: str = "amqp://localhost"
    prefetch: int = 16


queued = DynamicConfig(Queue, key="queue").file("config.toml")
prefetch: int = queued.init_and_current().prefetch


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

# `assert_type` rather than an annotated assignment: `Any` satisfies any
# annotation silently, so only this fails when the model's own type is
# lost on the way through `config`. It was — `Model.config` used to be
# `DynamicConfig[Any]`, which made every call through it untyped.
assert_type(Typed.current(), Typed)
assert_type(Typed.config, DynamicConfig[Typed])
assert_type(Typed.config.current(), Typed)
assert_type(Typed.config.try_current(), Optional[Typed])
assert_type(Typed.config.snapshot(), Snapshot)


async def use_the_decorated_configuration() -> None:
    """The async surface is reached through `config`, and stays typed."""
    await Typed.config.init_async()
    assert_type(await Typed.config.load_async(), Typed)
    assert_type(await Typed.config.changed_async(timeout=1), Optional[Typed])

    async for model in Typed.config.changes():
        assert_type(model, Typed)
        break

    async with Typed.config.running_async(watch=False) as started:
        assert_type(started, Typed)


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


# ── A store whose fetch is a coroutine ─────────────────────────────────


class OurAsyncService(AsyncRemoteSource):
    async def fetch(self) -> tuple[str, Format]:
        return '{"db": {"port": 5432}}', Format.JSON

    def describe(self) -> str:
        return "our async service"


async_remote: DynamicConfig[Database] = DynamicConfig(Database, key="db").remote(
    OurAsyncService()
)


# ── Dispatch, backpressure and the async hooks ─────────────────────────


async def reconnect(previous: Optional[Database], current: Database) -> None:
    del previous, current


async def use_the_async_surface() -> None:
    await async_remote.refresh_remote_async()

    dispatched: HookGuard = config.on_reload(
        resize, dispatch=Dispatch.EXECUTOR, backpressure=Backpressure.LATEST
    )
    dispatched.close()

    asynchronous: HookGuard = config.on_reload_async(
        reconnect, backpressure=Backpressure.CANCEL_PREVIOUS
    )
    asynchronous.close()

    by_path: HookGuard = config.on_change_async("port")(reconnect)
    by_path.close()

    async for model in config.changes():
        _: str = model.host
        break

    async for event in config.events(failure_poll=1.0):
        generation: int = event.generation
        del generation
        break

    async with config.running_async() as started:
        del started

    async with config.watching_async() as watching:
        del watching


# ── A group of configurations ──────────────────────────────────────────


class Sidecar(BaseModel):
    url: str = "redis://localhost"


sidecar: DynamicConfig[Sidecar] = DynamicConfig(Sidecar, key="sidecar")
group = ConfigGroup(config, sidecar, concurrency=2)
group.init()
group.reload_atomic()
generations: dict[str, int] = group.generations()
statuses: dict[str, ConfigStatus] = group.status()
members: tuple[DynamicConfig[object], ...] = group.configs


async def use_the_group() -> None:
    await group.init_async()
    await group.reload_atomic_async()

    async with group.running_async():
        pass


configure_executor(2)

with executor(workers=1):
    pass

with config.running(watch=False) as loaded:
    started_with: str = loaded.host
    del started_with
