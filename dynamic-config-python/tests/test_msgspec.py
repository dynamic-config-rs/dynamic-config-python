"""A `msgspec.Struct` as the schema.

The fifth kind of declaration, and the one whose library answers three
questions differently from the rest: it has no secret *type*, it raises a
message rather than a structured report, and two of its messages quote
the value they refused. Each of those has a test here, next to the
ordinary surface — the same layers, watcher, cache and diagnostics every
other schema gets, asserted once so that "it works the same" is a claim
this file can back.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Optional

import pytest

msgspec = pytest.importorskip("msgspec", reason="the [msgspec] extra is not installed")

from typing import Annotated  # noqa: E402

from dynamic_config import (  # noqa: E402
    DynamicConfig,
    InvalidError,
    changed_paths,
    secret_paths,
)


class Level(enum.Enum):
    DEBUG = "debug"
    INFO = "info"


class Pool(msgspec.Struct):
    max_size: int = 8
    timeout: float = 5.0


class Credentials(msgspec.Struct):
    user: str = "app"
    password: Annotated[str, msgspec.Meta(extra={"secret": True})] = ""


class Database(msgspec.Struct):
    host: str = "localhost"
    port: int = 5432
    tls: bool = False
    level: Level = Level.INFO
    password: Annotated[str, msgspec.Meta(extra={"secret": True})] = ""
    pool: Pool = msgspec.field(default_factory=Pool)
    credentials: Credentials = msgspec.field(default_factory=Credentials)
    replicas: Optional[list[str]] = None


class Strict(msgspec.Struct, forbid_unknown_fields=True):
    host: str = "localhost"


class Renamed(msgspec.Struct, rename="camel"):
    max_size: int = 8


class Required(msgspec.Struct):
    host: str


def write(document: str, name: str = "config.toml") -> None:
    Path(name).write_text(document)


def load(model: type, document: str, key: str = "db"):  # type: ignore[no-untyped-def]
    write(document)

    config = DynamicConfig(model, key=key).file("config.toml")
    config.init()

    return config.current()


# ── The ordinary surface, which is the whole point ─────────────────────


def test_a_struct_is_a_schema(workspace: Path) -> None:
    loaded = load(
        Database,
        '[db]\nhost = "db.internal"\nport = 6543\ntls = true\nlevel = "debug"\n\n'
        "[db.pool]\nmax_size = 32\n",
    )

    assert loaded.host == "db.internal"
    assert loaded.port == 6543
    assert loaded.tls is True
    assert loaded.level is Level.DEBUG
    assert loaded.pool.max_size == 32
    assert loaded.pool.timeout == 5.0, "a default the file did not mention"
    assert isinstance(loaded.pool, Pool), "nested structs are built, not left as dicts"


def test_defaults_and_factories_fill_what_a_file_omits(workspace: Path) -> None:
    loaded = load(Database, "[db]\n")

    assert loaded.host == "localhost"
    assert loaded.pool == Pool()
    assert loaded.replicas is None


def test_the_environment_reaches_a_struct_too(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DCTEST_DB_PORT", "9999")
    monkeypatch.setenv("DCTEST_DB_POOL__MAX_SIZE", "64")
    write('[db]\nhost = "from-file"\n')

    config = DynamicConfig(Database, key="db").file("config.toml").env("DCTEST_")
    config.init()

    assert config.current().port == 9999
    assert config.current().pool.max_size == 64
    assert config.current().host == "from-file"


def test_layers_and_reload_work_as_they_do_everywhere(workspace: Path) -> None:
    write('[db]\nhost = "first"\n')

    config = DynamicConfig(Database, key="db").file("config.toml")
    config.init()

    assert config.current().host == "first"

    write('[db]\nhost = "second"\n')
    config.reload()

    assert config.current().host == "second"


def test_a_lax_conversion_is_what_configuration_needs(workspace: Path) -> None:
    """`strict=False`, and the reason for it in one assertion.

    An environment variable is a string. A struct declaring `port: int`
    and a file writing `"6543"` is the same widening every other schema
    here performs, and strict decoding would refuse it.
    """
    loaded = load(Database, '[db]\nport = "6543"\n')

    assert loaded.port == 6543

    assert load(Database, "[db]\n[db.pool]\ntimeout = 1\n").pool.timeout == 1.0, (
        "and an integer written for a float widens, as it does everywhere else"
    )


# ── Refusals ───────────────────────────────────────────────────────────


def test_a_wrong_type_is_a_refused_load(workspace: Path) -> None:
    with pytest.raises(InvalidError, match="host"):
        load(Database, "[db]\nhost = [1, 2]\n")


def test_a_missing_required_field_is_named(workspace: Path) -> None:
    with pytest.raises(InvalidError, match="host"):
        load(Required, "[db]\n")


def test_unknown_keys_are_the_declaration_s_business(workspace: Path) -> None:
    """Ignored by msgspec; `forbid_unknown_fields=True` refuses them.

    Neither is imposed by this binding — which is the point. The struct
    says what it wants, exactly as a Pydantic model's `extra` does.
    """
    assert load(Database, '[db]\nhost = "h"\nstray = 1\n').host == "h"

    with pytest.raises(InvalidError, match="stray"):
        load(Strict, '[db]\nhost = "h"\nstray = 1\n')


def test_a_refusal_carries_an_empty_errors_list(workspace: Path) -> None:
    """The decision, asserted: nothing is invented for `errors`.

    msgspec raises a message, not a report, so there is nothing
    structured to hand over — and the attribute is present and empty
    rather than absent, so a program that reads it does not have to know
    which schema library the configuration was declared with.
    """
    write("[db]\nhost = [1, 2]\n")
    config = DynamicConfig(Database, key="db").file("config.toml")

    with pytest.raises(InvalidError) as failure:
        config.init()

    assert failure.value.errors == []
    assert "host" in str(failure.value)


# ── The values that must not travel ────────────────────────────────────


def test_meta_extra_declares_a_secret(workspace: Path) -> None:
    assert secret_paths(Database) == ["password", "credentials.password"]


def test_a_secret_under_a_container_redacts_the_whole_field(workspace: Path) -> None:
    """The case the review found, and the direction it has to be wrong in.

    `users.password` names nothing the redaction can walk to — it would
    have to index a list — so the *containing* field is redacted whole.
    Losing the usernames from a cache costs a diagnostic; keeping the
    passwords in one costs rather more.
    """

    class Tenants(msgspec.Struct):
        users: list[Credentials] = msgspec.field(default_factory=list)
        tenants: dict[str, Credentials] = msgspec.field(default_factory=dict)
        maybe: Optional[Credentials] = None

    assert secret_paths(Tenants) == ["users", "tenants", "maybe.password"]

    write('[db]\n[[db.users]]\nuser = "app"\npassword = "hunter2"\n')

    config = (
        DynamicConfig(Tenants, key="db")
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert "hunter2" not in Path("cache.json").read_text()


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


def test_secrets_may_also_be_named_at_the_configuration(workspace: Path) -> None:
    """`secrets=` adds to the declaration rather than replacing it."""
    write('[db]\nhost = "h"\npassword = "hunter2"\n')

    config = (
        DynamicConfig(Database, key="db", secrets=["host"])
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    written = Path("cache.json").read_text()

    assert "hunter2" not in written, "the declared one"
    assert '"h"' not in written, "and the named one"


def test_an_enum_refusal_does_not_repeat_the_value(workspace: Path) -> None:
    """The value msgspec quotes here, taken back out at this boundary.

    ``Invalid enum value 'x'`` is one of two messages msgspec writes the
    data into, and a `Level` could as easily be a password: the rule is
    that no value reaches a diagnostic, whichever library wrote the
    sentence.
    """
    with pytest.raises(InvalidError) as failure:
        load(Database, '[db]\nlevel = "PLANTED-SECRET"\n')

    assert "PLANTED-SECRET" not in str(failure.value)
    assert "level" in str(failure.value), "the path survives, because it is a name"


def test_a_secret_field_s_value_stays_out_of_its_own_refusal(
    workspace: Path,
) -> None:
    """The general net, not the two known shapes.

    A message that quotes the offending value has that value taken out by
    resolving the path msgspec reported — so a message shape msgspec
    grows later cannot leak through a list of prefixes nobody updated.
    """
    from dynamic_config._msgspec import scrub

    message = "Expected `str`, got `int` - at `$.password`"
    planted = "PLANTED-SECRET"

    assert planted not in scrub(
        f"Invalid value '{planted}' - at `$.password`", {"password": planted}
    )
    assert planted not in scrub(
        f"Something new about {planted!r} - at `$.password`", {"password": planted}
    )
    assert scrub(message, {"password": 1}) == message, "a value-free message is kept"


# ── Names a file writes ────────────────────────────────────────────────


def test_a_renamed_field_is_known_by_the_name_a_file_writes(
    workspace: Path,
) -> None:
    """`rename="camel"` moves the key, and the field list follows it."""
    loaded = load(Renamed, "[db]\nmaxSize = 32\n")

    assert loaded.max_size == 32

    config = DynamicConfig(Renamed, key="db").file("config.toml")
    write("[db]\nmax_size = 32\n")
    config.init()

    report = config.check()

    assert any("max_size" in str(unknown) for unknown in report.unknown), (
        "the Python name is not the key msgspec decodes, so a file using it "
        "is reporting an unknown key rather than setting the field"
    )


def test_changed_paths_works_between_struct_instances(workspace: Path) -> None:
    before = Database(host="one", password="a")
    after = Database(host="two", password="b")

    moved = {change.path for change in changed_paths(before, after)}

    assert "host" in moved
    assert "password" in moved


def test_a_diff_reports_the_key_a_file_writes(workspace: Path) -> None:
    """And not the Python name, which no source and no other path uses."""
    moved = {change.path for change in changed_paths(Renamed(1), Renamed(2))}

    assert moved == {"maxSize"}
