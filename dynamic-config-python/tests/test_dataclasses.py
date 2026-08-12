"""A plain `dataclasses.dataclass` as the schema, with no Pydantic in sight.

The package's base install has no dependencies, so this is the path a
`pip install dynamic-config-py` user is on. What it promises is
*structural* validation — required fields, unknown keys, nested
dataclasses, and each value against the type its field declares — and
explicitly not coercion. These tests pin both halves, including the
refusals, because a schema that quietly accepts the wrong type is worse
than one that has no opinion.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional, Union

import pytest

from dynamic_config import DynamicConfig, InvalidError, secret_paths


@dataclass
class Pool:
    max_size: int = 8
    timeout: float = 5.0


class Level(enum.Enum):
    DEBUG = "debug"
    INFO = "info"


@dataclass
class Credentials:
    user: str = "app"
    password: str = field(default="", metadata={"secret": True})


@dataclass
class Service:
    host: str = "localhost"
    credentials: Credentials = field(default_factory=Credentials)


@dataclass
class Scheduled:
    on: datetime.date = datetime.date(1970, 1, 1)
    at: datetime.datetime = datetime.datetime(1970, 1, 1)


class Unbuildable:
    """A type that neither matches nor parses: the honest refusal case."""

    def __init__(self, first: int, second: int) -> None:
        """Two required arguments, so one value cannot build it."""
        self.first = first
        self.second = second


@dataclass
class Impossible:
    thing: Unbuildable = field(default_factory=lambda: Unbuildable(0, 0))


@dataclass
class Modes:
    mode: Literal["read", "write"] = "read"


@dataclass
class Flagged:
    flag: Literal[True, False] = False


@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432
    tls: bool = False
    password: str = field(default="", metadata={"secret": True})
    pool: Pool = field(default_factory=Pool)


def write(document: str, name: str = "config.toml") -> None:
    Path(name).write_text(document)


def load(model: type, document: str, key: str = "db"):  # type: ignore[no-untyped-def]
    write(document)

    config = DynamicConfig(model, key=key).file("config.toml")
    config.init()

    return config.current()


# ── The happy path ─────────────────────────────────────────────────────


def test_a_dataclass_is_a_schema(workspace: Path) -> None:
    loaded = load(
        Database,
        '[db]\nhost = "db.internal"\nport = 6543\ntls = true\n\n'
        "[db.pool]\nmax_size = 32\n",
    )

    assert loaded.host == "db.internal"
    assert loaded.port == 6543
    assert loaded.tls is True
    assert loaded.pool.max_size == 32
    assert loaded.pool.timeout == 5.0, "a default the file did not mention"
    assert isinstance(loaded.pool, Pool), (
        "nested dataclasses are built, not left as dicts"
    )


def test_defaults_and_factories_fill_what_a_file_omits(workspace: Path) -> None:
    loaded = load(Database, "[db]\n")

    assert loaded.host == "localhost"
    assert loaded.pool == Pool()


def test_the_environment_reaches_a_dataclass_too(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCTEST_DB_PORT", "9999")
    monkeypatch.setenv("DCTEST_DB_POOL__MAX_SIZE", "64")
    write('[db]\nhost = "from-file"\n')

    config = DynamicConfig(Database, key="db").file("config.toml").env("DCTEST_")
    config.init()

    assert config.current().port == 9999, "the env layer types its values"
    assert config.current().pool.max_size == 64


def test_a_dataclass_reloads_and_keeps_provenance(workspace: Path) -> None:
    write('[db]\nhost = "one"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    assert config.current().host == "one"
    assert "config.toml" in str(config.source_of("host"))

    write('[db]\nhost = "two"\n')
    config.reload()

    assert config.current().host == "two"
    assert config.generation == 2


# ── The refusals ───────────────────────────────────────────────────────


def test_a_required_field_nobody_supplies_is_refused(workspace: Path) -> None:
    @dataclass
    class Strict:
        host: str
        port: int = 1

    with pytest.raises(InvalidError, match="host"):
        load(Strict, "[db]\nport = 2\n")


def test_a_key_the_dataclass_never_heard_of_is_refused(workspace: Path) -> None:
    with pytest.raises(InvalidError, match="stray"):
        load(Database, '[db]\nhost = "h"\nstray = 1\n')


def test_a_wrong_type_names_the_field_and_the_types(workspace: Path) -> None:
    with pytest.raises(InvalidError) as failure:
        load(Database, '[db]\nport = "not-a-number"\n')

    message = str(failure.value)
    assert "port" in message
    assert "int" in message
    assert "not-a-number" not in message, "a diagnostic never carries the value"


def test_a_bool_is_not_an_int_and_an_int_is_not_a_bool(workspace: Path) -> None:
    @dataclass
    class Flags:
        enabled: bool = False
        count: int = 0

    assert load(Flags, "[db]\nenabled = true\ncount = 3\n").enabled is True

    with pytest.raises(InvalidError, match="enabled"):
        load(Flags, "[db]\nenabled = 1\n")

    with pytest.raises(InvalidError, match="count"):
        load(Flags, "[db]\ncount = true\n")


def test_a_rejected_reload_keeps_the_previous_model(workspace: Path) -> None:
    write('[db]\nhost = "good"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    good = config.current()

    write('[db]\nport = "nonsense"\n')

    with pytest.raises(InvalidError):
        config.reload()

    assert config.current() is good
    assert config.generation == 1


def test_a_nested_table_that_is_not_a_table(workspace: Path) -> None:
    with pytest.raises(InvalidError, match="pool"):
        load(Database, "[db]\npool = 3\n")


# ── The types it can and cannot build ──────────────────────────────────


def test_optionals_unions_and_containers(workspace: Path) -> None:
    @dataclass
    class Shapes:
        maybe: Optional[int] = None
        either: Union[int, str] = 0
        hosts: list = field(default_factory=list)
        ports: list[int] = field(default_factory=list)
        labels: dict[str, str] = field(default_factory=dict)

    loaded = load(
        Shapes,
        '[db]\neither = "text"\nhosts = ["a", "b"]\nports = [1, 2]\n\n'
        '[db.labels]\nenv = "prod"\n',
    )

    assert loaded.maybe is None
    assert loaded.either == "text"
    assert loaded.hosts == ["a", "b"]
    assert loaded.ports == [1, 2]
    assert loaded.labels == {"env": "prod"}


def test_a_list_element_of_the_wrong_type_is_refused(workspace: Path) -> None:
    @dataclass
    class Ports:
        ports: list[int] = field(default_factory=list)

    with pytest.raises(InvalidError, match=r"ports\[1\]"):
        load(Ports, '[db]\nports = [1, "two"]\n')


def test_an_enum_takes_its_members_value(workspace: Path) -> None:
    @dataclass
    class Logging:
        level: Level = Level.INFO

    assert load(Logging, '[db]\nlevel = "debug"\n').level is Level.DEBUG

    with pytest.raises(InvalidError, match="level"):
        load(Logging, '[db]\nlevel = "shouting"\n')


def test_types_that_parse_their_own_text_are_built(workspace: Path) -> None:
    @dataclass
    class Addressed:
        identifier: uuid.UUID = field(default_factory=lambda: uuid.UUID(int=0))
        directory: Path = Path()
        ratio: Decimal = Decimal("1")

    loaded = load(
        Addressed,
        '[db]\nidentifier = "12345678-1234-5678-1234-567812345678"\n'
        'directory = "/etc/app"\nratio = "1.5"\n',
    )

    assert loaded.identifier == uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert loaded.directory == Path("/etc/app")
    assert loaded.ratio == Decimal("1.5")


def test_dates_and_times_parse_from_their_own_text(workspace: Path) -> None:
    """`fromisoformat` is a classmethod, and text is what a file carries.

    Both spellings reach it: a TOML *native* date, which figment hides
    behind a marker the binding unwraps, and an ISO string from anywhere
    else.
    """
    native = load(Scheduled, "[db]\non = 2026-01-01\nat = 2026-01-01T10:30:00\n")

    assert native.on == datetime.date(2026, 1, 1)
    assert native.at == datetime.datetime(2026, 1, 1, 10, 30)

    quoted = load(Scheduled, '[db]\non = "2026-02-02"\n')

    assert quoted.on == datetime.date(2026, 2, 2)

    with pytest.raises(InvalidError, match="on"):
        load(Scheduled, '[db]\non = "the second of February"\n')


def test_a_type_that_neither_matches_nor_parses_says_so(workspace: Path) -> None:
    """Silence would be the bug: the field would hold a string.

    A configuration value is text, a number or a table. A type that
    cannot be built from one of those is a type this adapter refuses
    rather than assigns.
    """
    with pytest.raises(InvalidError) as failure:
        load(Impossible, '[db]\nthing = "whatever"\n')

    message = str(failure.value)
    assert "thing" in message
    assert "Unbuildable" in message
    assert "whatever" not in message


def test_an_int_widens_to_a_float_and_not_the_other_way(workspace: Path) -> None:
    @dataclass
    class Ratios:
        ratio: float = 0.0
        count: int = 0

    assert load(Ratios, "[db]\nratio = 1\n").ratio == 1.0

    with pytest.raises(InvalidError, match="count"):
        load(Ratios, "[db]\ncount = 1.5\n")


# ── Secrets ────────────────────────────────────────────────────────────


def test_metadata_declares_a_secret(workspace: Path) -> None:
    assert secret_paths(Database) == ["password"]


def test_a_secret_inside_a_nested_dataclass_is_found(workspace: Path) -> None:
    assert secret_paths(Service) == ["credentials.password"]


def test_annotations_a_class_cannot_resolve_are_not_checked(
    workspace: Path,
) -> None:
    """The one limitation worth knowing, asserted rather than implied.

    `typing.get_type_hints` resolves a class's annotations in the module
    where it was defined. A dataclass declared *inside a function* names
    types that module cannot see, so the annotations stay strings and
    this adapter has nothing to check them against — it fills the field
    rather than guessing. Pydantic meets the same wall and answers it
    with `model_rebuild()`; here the answer is to declare configuration
    dataclasses at module level, which is where they belong anyway.
    """

    @dataclass
    class LocalPool:
        max_size: int = 8

    @dataclass
    class LocalService:
        pool: LocalPool = field(default_factory=LocalPool)

    loaded = load(LocalService, "[db]\n[db.pool]\nmax_size = 32\n")

    assert loaded.pool == {"max_size": 32}, (
        "a dict rather than a LocalPool: the annotation naming it could not "
        "be resolved from the module, so there was nothing to build"
    )

    # Builtin annotations still resolve, because the module can see them —
    # so the checks that matter most keep working even here.
    @dataclass
    class LocalPort:
        port: int = 1

    with pytest.raises(InvalidError, match="port"):
        load(LocalPort, '[db]\nport = "not-a-number"\n')


def test_a_declared_secret_never_reaches_a_diagnostic(workspace: Path) -> None:
    write('[db]\nhost = "h"\npassword = "hunter2"\n')

    config = (
        DynamicConfig(Database, key="db")
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert config.current().password == "hunter2", "the program still gets its value"
    assert "hunter2" not in str(config.explain("password"))
    assert "hunter2" not in Path("cache.json").read_text()
    assert "hunter2" not in repr(config.snapshot())


def test_changed_paths_works_between_dataclass_instances(workspace: Path) -> None:
    from dynamic_config import changed_paths

    before = Database(host="one", password="a")
    after = Database(host="two", password="b")

    changes = changed_paths(before, after)
    moved = {change.path for change in changes}

    assert moved == {"host", "password"}
    assert "one" not in str(changes)
    assert "a" not in {change.path for change in changes}


# ── The rest of the surface, unchanged ─────────────────────────────────


def test_the_decorator_takes_a_dataclass(workspace: Path) -> None:
    from dynamic_config import dynamic_config

    write('[db]\nhost = "decorated"\n')

    @dynamic_config(key="db", files=["config.toml"])
    @dataclass
    class Decorated:
        host: str = "localhost"

    Decorated.config.init()

    assert Decorated.current().host == "decorated"


def test_runtime_layers_and_diagnostics(workspace: Path) -> None:
    write('[db]\nhost = "file"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.set_default("port", 1111)
    config.set_override("host", "overridden")
    config.init()

    assert config.current().host == "overridden"
    assert config.current().port == 1111

    report = config.check()
    assert report.is_clean, [unknown.path for unknown in report.unknown]

    assert config.snapshot().to_dict()["host"] == "overridden"


def test_set_defaults_accepts_a_dataclass_instance(workspace: Path) -> None:
    write("[db]\n")

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.set_defaults(Database(host="from-instance", port=2222))
    config.init()

    assert config.current().host == "from-instance"
    assert config.current().port == 2222


def test_replace_takes_an_instance_of_the_dataclass(workspace: Path) -> None:
    write('[db]\nhost = "file"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()
    config.replace(Database(host="replaced"))

    assert config.current().host == "replaced"

    with pytest.raises(TypeError):
        config.replace(Pool())  # type: ignore[arg-type]


def test_something_that_is_not_a_schema_is_refused(workspace: Path) -> None:
    class Plain:
        host: str = "localhost"

    with pytest.raises(TypeError, match="schema class"):
        DynamicConfig(Plain, key="db")

    with pytest.raises(TypeError, match="schema class"):
        DynamicConfig(dict, key="db")  # type: ignore[type-var]


def test_a_dataclass_instance_is_not_a_schema(workspace: Path) -> None:
    with pytest.raises(TypeError, match="schema class"):
        DynamicConfig(Database(), key="db")  # type: ignore[arg-type]


def test_dataclasses_are_not_frozen_by_this_library(workspace: Path) -> None:
    """A frozen dataclass works too; the library does not require either."""

    @dataclasses.dataclass(frozen=True)
    class Frozen:
        host: str = "localhost"

    loaded = load(Frozen, '[db]\nhost = "immutable"\n')

    assert loaded.host == "immutable"

    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.host = "no"  # type: ignore[misc]


def test_a_literal_field_accepts_only_its_own_values(workspace: Path) -> None:
    """`Literal` is a set of values, and the adapter promises to check it.

    It reached the container branch and fell through unchecked, so
    `mode = "delete"` installed cleanly against
    `Literal["read", "write"]`.
    """
    assert load(Modes, '[db]\nmode = "write"\n').mode == "write"

    with pytest.raises(InvalidError, match="mode"):
        load(Modes, '[db]\nmode = "delete"\n')


def test_a_literal_keeps_bool_and_int_apart(workspace: Path) -> None:
    with pytest.raises(InvalidError, match="flag"):
        load(Flagged, "[db]\nflag = 1\n")

    assert load(Flagged, "[db]\nflag = true\n").flag is True
