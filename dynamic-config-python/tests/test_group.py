"""Several configurations under one lifecycle, and the atomic reload."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from dynamic_config import ConfigGroup, DynamicConfig, InvalidError


class Database(BaseModel):
    host: str
    port: int


class Cache(BaseModel):
    url: str
    ttl: int


def write(port: int, ttl: int | str = 60) -> None:
    Path("config.json").write_text(
        json.dumps(
            {
                "db": {"host": "h", "port": port},
                "cache": {"url": "redis://c", "ttl": ttl},
            }
        )
    )


def pair() -> tuple[DynamicConfig[Database], DynamicConfig[Cache]]:
    return (
        DynamicConfig(Database, key="db").file("config.json"),
        DynamicConfig(Cache, key="cache").file("config.json"),
    )


def test_a_group_needs_configurations_and_distinct_keys(workspace: Path) -> None:
    write(1)
    database, cache = pair()

    with pytest.raises(ValueError, match="nothing to do"):
        ConfigGroup()

    with pytest.raises(ValueError, match="share a key"):
        ConfigGroup(database, database)

    with pytest.raises(ValueError, match="at least 1"):
        ConfigGroup(database, cache, concurrency=0)


def test_a_group_initialises_every_member(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)

    group.init()

    assert database.current().port == 1
    assert cache.current().ttl == 60
    assert group.generations() == {"db": 1, "cache": 1}
    assert sorted(group.status()) == ["cache", "db"]
    assert len(group) == 2
    assert [config.key for config in group] == ["db", "cache"]


def test_a_refusing_member_stops_the_group(workspace: Path) -> None:
    write(1, ttl="forever")
    database, cache = pair()

    with pytest.raises(InvalidError):
        ConfigGroup(database, cache).init()

    assert database.try_current() is not None, "the one before it had already loaded"
    assert cache.try_current() is None


def test_an_atomic_reload_installs_all_or_none(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)
    group.init()

    write(2, ttl="forever")

    with pytest.raises(InvalidError):
        group.reload_atomic()

    assert database.current().port == 1, "a member that parsed still did not install"
    assert cache.current().ttl == 60
    assert group.generations() == {"db": 1, "cache": 1}, "not even the generation moved"

    write(3, ttl=90)
    group.reload_atomic()

    assert database.current().port == 3
    assert cache.current().ttl == 90
    assert group.generations() == {"db": 2, "cache": 2}


def test_a_plain_group_reload_is_per_member(workspace: Path) -> None:
    """The other half of the contract: `reload` is not `reload_atomic`."""
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)
    group.init()

    write(2, ttl="forever")

    with pytest.raises(InvalidError):
        group.reload()

    assert database.current().port == 2, "this one reloaded, on its own"
    assert cache.current().ttl == 60


def test_a_refusing_member_does_not_stop_the_reload_after_it(
    workspace: Path,
) -> None:
    """A member's refusal is its own business, and the group says so."""
    write(1)
    # The refusing one first, so what is under test is what happens after.
    cache = DynamicConfig(Cache, key="cache").file("config.json")
    database = DynamicConfig(Database, key="db").file("config.json")
    group = ConfigGroup(cache, database)
    group.init()

    write(2, ttl="forever")

    with pytest.raises(InvalidError):
        group.reload()

    assert cache.current().ttl == 60, "refused, and still serving what it had"
    assert database.current().port == 2, "and the member after it still reloaded"


async def test_the_same_holds_for_the_async_reload(workspace: Path) -> None:
    write(1)
    cache = DynamicConfig(Cache, key="cache").file("config.json")
    database = DynamicConfig(Database, key="db").file("config.json")
    group = ConfigGroup(cache, database, concurrency=2)
    await group.init_async()

    write(2, ttl="forever")

    with pytest.raises(InvalidError):
        await group.reload_async()

    assert cache.current().ttl == 60
    assert database.current().port == 2


async def test_the_async_twins_load_the_same_way(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)

    await group.init_async()

    assert database.current().port == 1

    write(2)
    await group.reload_async()

    assert database.current().port == 2

    write(3)
    await group.reload_atomic_async()

    assert database.current().port == 3
    assert cache.current().ttl == 60


def test_concurrency_loads_in_parallel_and_reports_the_first_failure(
    workspace: Path,
) -> None:
    write(1)
    database, cache = pair()

    ConfigGroup(database, cache, concurrency=2).init()

    assert database.current().port == 1
    assert cache.current().ttl == 60

    write(2, ttl="forever")
    broken = DynamicConfig(Cache, key="cache").file("config.json")

    with pytest.raises(InvalidError):
        ConfigGroup(broken, concurrency=2).init()


class Slow(DynamicConfig[Database]):
    """A member whose load takes long enough to be counted."""

    inside = 0
    peak = 0

    async def init_async(self) -> None:
        Slow.inside += 1
        Slow.peak = max(Slow.peak, Slow.inside)

        await asyncio.sleep(0.05)
        Slow.inside -= 1

        await super().init_async()


async def test_concurrency_bounds_how_many_load_at_once(workspace: Path) -> None:
    """Two at a time means two at a time, not one thread per member."""
    write(1)
    Slow.inside = Slow.peak = 0

    group = ConfigGroup(
        *[Slow(Database, key=f"db{index}").file("config.json") for index in range(6)],
        concurrency=2,
    )

    with pytest.raises(InvalidError):
        # Every member reads the `db0`…`db5` sections, which the document
        # does not have — the load is beside the point here, the counting
        # is not.
        await group.init_async()

    assert Slow.peak == 2, f"{Slow.peak} loaded at once, with concurrency=2"


async def test_no_concurrency_loads_them_one_at_a_time(workspace: Path) -> None:
    write(1)
    Slow.inside = Slow.peak = 0

    group = ConfigGroup(
        *[Slow(Database, key=f"db{index}").file("config.json") for index in range(3)]
    )

    with pytest.raises(InvalidError):
        await group.init_async()

    assert Slow.peak == 1, "the default is sequential, and stays sequential"


def test_watching_starts_and_stops_every_member(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)
    group.init()

    with group.watching(debounce=0.05) as watching:
        assert watching is group
        assert len(group._watches) == 2

    assert group._watches == [], "the block stopped both"

    group.watch(debounce=0.05)
    group.stop()
    group.stop()  # idempotent

    assert group._watches == []


def test_a_watched_group_reloads_its_members(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)
    group.init()

    with group.watching(debounce=0.05):
        write(2)

        for _ in range(100):
            if database.generation > 1 and cache.generation > 1:
                break

            import time

            time.sleep(0.05)

    assert database.current().port == 2


def test_running_is_init_then_watch_then_stop(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)

    with group.running(watch=True, debounce=0.05) as running:
        assert running is group
        assert database.current().port == 1
        assert len(group._watches) == 2

    assert group._watches == []


async def test_running_async_is_the_lifespan_shape(workspace: Path) -> None:
    write(1)
    database, cache = pair()
    group = ConfigGroup(database, cache)

    async with group.running_async(debounce=0.05):
        assert database.current().port == 1
        assert len(group._watches) == 2

    assert group._watches == []


def test_a_group_repr_names_its_members(workspace: Path) -> None:
    write(1)
    database, cache = pair()

    assert repr(ConfigGroup(database, cache)) == "ConfigGroup(db, cache)"
