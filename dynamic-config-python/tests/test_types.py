"""Every shape a Pydantic model can ask for, across the boundary.

Two directions have to hold. Coming *in*, the resolved tree becomes
Python data that Pydantic can validate into whatever the model declares —
enums, dates, UUIDs, decimals, unions, nested models, containers of them.
Going *out*, a value the caller supplies (a default, an override) has to
survive the trip in the other direction with its type intact.

The interesting failures are silent ones: an integer arriving as a float,
a bool arriving as `1`, a large `u64` losing precision. Those get their
own tests.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal
from ipaddress import IPv4Address
from pathlib import Path, PurePosixPath
from typing import Literal, Optional, Union

import pytest
from pydantic import BaseModel, Field, SecretStr

from dynamic_config import DynamicConfig


class Level(str, enum.Enum):
    DEBUG = "debug"
    INFO = "info"
    ERROR = "error"


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 10


class Endpoint(BaseModel):
    host: str
    port: int = 80


class Everything(BaseModel):
    # Scalars
    text: str
    count: int
    ratio: float
    enabled: bool
    nothing: Optional[str] = None

    # Enumerations, by value
    level: Level = Level.INFO
    priority: Priority = Priority.LOW
    mode: Literal["read", "write"] = "read"

    # Standard library types Pydantic coerces from strings
    started: dt.datetime = dt.datetime(2020, 1, 1)
    day: dt.date = dt.date(2020, 1, 1)
    window: dt.timedelta = dt.timedelta(seconds=30)
    identifier: uuid.UUID = uuid.UUID(int=0)
    amount: Decimal = Decimal("0")
    address: IPv4Address = IPv4Address("127.0.0.1")
    where: Path = Path("/tmp")
    posix: PurePosixPath = PurePosixPath("/tmp")

    # Containers
    names: list[str] = Field(default_factory=list)
    ports: set[int] = Field(default_factory=set)
    pair: tuple[str, int] = ("a", 1)
    labels: dict[str, str] = Field(default_factory=dict)

    # Composition
    primary: Endpoint = Endpoint(host="localhost")
    replicas: list[Endpoint] = Field(default_factory=list)
    by_region: dict[str, Endpoint] = Field(default_factory=dict)
    either: Union[int, str] = 0
    secret: SecretStr = SecretStr("")


DOCUMENT = """
[svc]
text = "hello"
count = 42
ratio = 0.5
enabled = true
level = "error"
priority = 10
mode = "write"
started = "2024-03-01T12:30:00"
day = "2024-03-01"
window = 90
identifier = "123e4567-e89b-12d3-a456-426614174000"
amount = "10.25"
address = "10.0.0.1"
where = "/etc/app"
posix = "/etc/app"
names = ["a", "b"]
ports = [1, 2, 2]
pair = ["x", 9]
either = "a-string"
secret = "planted"

[svc.labels]
team = "platform"

[svc.primary]
host = "primary.internal"
port = 5432

[[svc.replicas]]
host = "replica-1"

[[svc.replicas]]
host = "replica-2"
port = 5433

[svc.by_region.eu]
host = "eu.internal"
"""


@pytest.fixture
def loaded(workspace: Path) -> DynamicConfig[Everything]:
    Path("config.toml").write_text(DOCUMENT)
    config = DynamicConfig(Everything, key="svc").file("config.toml")
    config.init()

    return config


def test_scalars_keep_their_types(loaded: DynamicConfig[Everything]) -> None:
    value = loaded.current()

    assert value.text == "hello"
    assert value.count == 42
    assert isinstance(value.count, int)
    assert value.ratio == 0.5
    assert isinstance(value.ratio, float)
    assert value.enabled is True
    assert value.nothing is None


def test_enumerations_arrive_as_members(loaded: DynamicConfig[Everything]) -> None:
    value = loaded.current()

    assert value.level is Level.ERROR
    assert value.priority is Priority.HIGH
    assert value.mode == "write"


def test_standard_library_types_are_coerced(loaded: DynamicConfig[Everything]) -> None:
    value = loaded.current()

    assert value.started == dt.datetime(2024, 3, 1, 12, 30)
    assert value.day == dt.date(2024, 3, 1)
    assert value.window == dt.timedelta(seconds=90)
    assert value.identifier == uuid.UUID("123e4567-e89b-12d3-a456-426614174000")
    assert value.amount == Decimal("10.25")
    assert value.address == IPv4Address("10.0.0.1")
    assert value.where == Path("/etc/app")
    assert value.posix == PurePosixPath("/etc/app")


def test_containers_survive(loaded: DynamicConfig[Everything]) -> None:
    value = loaded.current()

    assert value.names == ["a", "b"]
    assert value.ports == {1, 2}
    assert value.pair == ("x", 9)
    assert value.labels == {"team": "platform"}


def test_nested_models_and_unions(loaded: DynamicConfig[Everything]) -> None:
    value = loaded.current()

    assert value.primary.host == "primary.internal"
    assert value.primary.port == 5432
    assert [replica.host for replica in value.replicas] == ["replica-1", "replica-2"]
    assert value.replicas[0].port == 80, "a nested default still applies"
    assert value.replicas[1].port == 5433
    assert value.by_region["eu"].host == "eu.internal"
    assert value.either == "a-string"
    assert value.secret.get_secret_value() == "planted"


def test_an_integer_does_not_become_a_float(workspace: Path) -> None:
    """The silent failure this conversion exists to avoid."""

    class Ports(BaseModel):
        port: int

    Path("config.json").write_text('{"svc": {"port": 5432}}')
    config = DynamicConfig(Ports, key="svc").file("config.json")
    config.init()

    assert isinstance(config.current().port, int)
    assert config.snapshot().to_dict()["port"] == 5432
    assert isinstance(config.snapshot().to_dict()["port"], int)


def test_a_bool_does_not_become_an_integer(workspace: Path) -> None:
    class Flags(BaseModel):
        enabled: bool

    Path("config.json").write_text('{"svc": {"enabled": true}}')
    config = DynamicConfig(Flags, key="svc").file("config.json")
    config.init()

    exported = config.snapshot().to_dict()["enabled"]

    assert exported is True, "a bool must not arrive as 1"


def test_large_integers_keep_their_precision(workspace: Path) -> None:
    class Big(BaseModel):
        below: int
        above: int

    # One below `i64::MAX`, one above it: the second is only expressible
    # as a `u64`, and a float would lose the last digits.
    Path("config.json").write_text(
        '{"svc": {"below": 9223372036854775806, "above": 18446744073709551615}}'
    )
    config = DynamicConfig(Big, key="svc").file("config.json")
    config.init()

    assert config.current().below == 9223372036854775806
    assert config.current().above == 18446744073709551615


def test_empty_containers_and_deep_nesting(workspace: Path) -> None:
    class Deep(BaseModel):
        empty_list: list[int]
        empty_map: dict[str, str]
        nested: dict[str, dict[str, list[int]]]

    Path("config.json").write_text(
        '{"svc": {"empty_list": [], "empty_map": {}, '
        '"nested": {"a": {"b": [1, 2, 3]}}}}'
    )
    config = DynamicConfig(Deep, key="svc").file("config.json")
    config.init()

    value = config.current()
    assert value.empty_list == []
    assert value.empty_map == {}
    assert value.nested["a"]["b"] == [1, 2, 3]


def test_unicode_keys_and_values(workspace: Path) -> None:
    class Unicode(BaseModel):
        greeting: str
        labels: dict[str, str]

    Path("config.json").write_text(
        '{"svc": {"greeting": "merhaba dünya", "labels": {"şehir": "İstanbul"}}}'
    )
    config = DynamicConfig(Unicode, key="svc").file("config.json")
    config.init()

    assert config.current().greeting == "merhaba dünya"
    assert config.current().labels["şehir"] == "İstanbul"


def test_values_supplied_from_python_keep_their_types(workspace: Path) -> None:
    """The other direction: defaults and overrides start as Python objects."""

    class Mixed(BaseModel):
        flag: bool
        count: int
        ratio: float
        name: str
        items: list[int]
        mapping: dict[str, int]
        missing: Optional[str]

    config = DynamicConfig(Mixed, key="svc")
    config.set_default("flag", True)
    config.set_default("count", 7)
    config.set_default("ratio", 1.5)
    config.set_default("name", "from-python")
    config.set_default("items", [1, 2, 3])
    config.set_default("mapping", {"a": 1})
    config.set_default("missing", None)
    config.init()

    value = config.current()

    assert value.flag is True
    assert value.count == 7
    assert isinstance(value.count, int)
    assert value.ratio == 1.5
    assert value.items == [1, 2, 3]
    assert value.mapping == {"a": 1}
    assert value.missing is None


def test_a_model_can_be_supplied_as_defaults(workspace: Path) -> None:
    config = DynamicConfig(Endpoint, key="svc")
    config.set_defaults(Endpoint(host="from-model", port=9999))
    config.init()

    assert config.current().host == "from-model"
    assert config.current().port == 9999


def test_an_enum_member_can_be_supplied_from_python(workspace: Path) -> None:
    class Logging(BaseModel):
        level: Level

    config = DynamicConfig(Logging, key="svc")
    config.set_default("level", Level.ERROR.value)
    config.init()

    assert config.current().level is Level.ERROR


def test_something_that_is_not_configuration_is_refused(workspace: Path) -> None:
    config = DynamicConfig(Endpoint, key="svc")

    with pytest.raises(TypeError) as failure:
        config.set_default("host", lambda: "a function")

    assert "not a configuration value" in str(failure.value)


def test_nan_and_infinity_are_refused(workspace: Path) -> None:
    config = DynamicConfig(Endpoint, key="svc")

    for value in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="not configuration values"):
            config.set_default("port", value)


def test_a_non_string_key_is_refused(workspace: Path) -> None:
    config = DynamicConfig(Endpoint, key="svc")

    with pytest.raises(TypeError) as failure:
        config.set_default("labels", {1: "one"})

    assert "keys" in str(failure.value)


def test_environment_variables_reach_every_scalar_shape(
    workspace: Path, monkeypatch
) -> None:
    class FromEnv(BaseModel):
        text: str
        count: int
        ratio: float
        enabled: bool
        level: Level

    monkeypatch.setenv("DCTEST_SVC_TEXT", "from-env")
    monkeypatch.setenv("DCTEST_SVC_COUNT", "11")
    monkeypatch.setenv("DCTEST_SVC_RATIO", "2.5")
    monkeypatch.setenv("DCTEST_SVC_ENABLED", "true")
    monkeypatch.setenv("DCTEST_SVC_LEVEL", "debug")

    config = DynamicConfig(FromEnv, key="svc").env("DCTEST_")
    config.init()

    value = config.current()
    assert value.text == "from-env"
    assert value.count == 11
    assert value.ratio == 2.5
    assert value.enabled is True
    assert value.level is Level.DEBUG


def test_values_that_are_types_survive_a_round_trip(workspace: Path) -> None:
    """`changed_paths` and `set_defaults` take a model back apart.

    Neither `model_dump()` nor `dataclasses.asdict` unwraps an enum, and
    a `Decimal`, a `date` and a `UUID` are not JSON scalars — so without
    a conversion for each, an audit line could not be produced for a
    model holding any of them. Every one of these raised a TypeError
    before there was one.
    """
    import datetime
    import ipaddress
    import uuid
    from decimal import Decimal

    from dynamic_config import changed_paths

    class Rich(BaseModel):
        level: Level = Level.INFO
        ratio: Decimal = Decimal("1")
        on: datetime.date = datetime.date(2020, 1, 1)
        where: Path = Path("/a")
        who: uuid.UUID = uuid.UUID(int=0)
        address: ipaddress.IPv4Address = ipaddress.IPv4Address("10.0.0.1")

    assert changed_paths(Rich(), Rich()) == []

    moved = [
        (Rich(level=Level.INFO), Rich(level=Level.DEBUG), "level"),
        (Rich(ratio=Decimal("1")), Rich(ratio=Decimal("2")), "ratio"),
        (
            Rich(on=datetime.date(2020, 1, 1)),
            Rich(on=datetime.date(2021, 1, 1)),
            "on",
        ),
        (Rich(where=Path("/a")), Rich(where=Path("/b")), "where"),
        (Rich(who=uuid.UUID(int=0)), Rich(who=uuid.UUID(int=1)), "who"),
        (
            Rich(address=ipaddress.IPv4Address("10.0.0.1")),
            Rich(address=ipaddress.IPv4Address("10.0.0.2")),
            "address",
        ),
    ]

    for before, after, path in moved:
        assert [change.path for change in changed_paths(before, after)] == [path]


def test_a_model_with_rich_values_can_seed_the_defaults(workspace: Path) -> None:
    from decimal import Decimal

    class Rich(BaseModel):
        level: Level = Level.INFO
        ratio: Decimal = Decimal("1")

    Path("config.toml").write_text("[svc]\n")

    config = DynamicConfig(Rich, key="svc").file("config.toml")
    config.set_defaults(Rich(level=Level.DEBUG, ratio=Decimal("2.5")))
    config.init()

    assert config.current().level is Level.DEBUG
    assert config.current().ratio == Decimal("2.5")
