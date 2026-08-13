"""The Pydantic class surface: whatever a model may be, it may be here.

`test_types.py` covers the *values* a field can hold. This file covers the
*classes* a schema can be — inheritance, `model_config`, validators,
aliases, `RootModel`, Pydantic dataclasses, generics, and
`pydantic_settings.BaseSettings` — because a binding that only accepts
plain `BaseModel` accepts a fraction of the models people already have.

`from_settings` has its own section at the end: a settings class is two
things, and only one of them is a schema.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Annotated, Generic, Literal, Optional, TypeVar, Union

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    RootModel,
    SecretStr,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

from dynamic_config import DynamicConfig, InvalidError, secret_paths

settings = pytest.importorskip("pydantic_settings")
BaseSettings = settings.BaseSettings
SettingsConfigDict = settings.SettingsConfigDict


def load(model: type, document: str, key: str = "s") -> object:
    """Writes a file, loads it through the binding, returns the model."""
    Path("config.toml").write_text(document)

    config = DynamicConfig(model, key=key).file("config.toml")
    config.init()

    return config.current()


# ── Inheritance ────────────────────────────────────────────────────────


def test_a_model_inherits_fields_from_every_ancestor(workspace: Path) -> None:
    class Base(BaseModel):
        host: str = "base"

    class Middle(Base):
        port: int = 1

    class Leaf(Middle):
        tls: bool = False

    loaded = load(Leaf, '[s]\nhost = "a"\nport = 2\ntls = true\n')

    assert (loaded.host, loaded.port, loaded.tls) == ("a", 2, True)


def test_a_plain_mixin_beside_a_model_is_fine(workspace: Path) -> None:
    class Describable:
        def describe(self) -> str:
            return "described"

    class WithMixin(Describable, BaseModel):
        port: int = 1

    loaded = load(WithMixin, "[s]\nport = 5\n")

    assert loaded.port == 5
    assert loaded.describe() == "described"


def test_an_override_in_a_subclass_wins(workspace: Path) -> None:
    class Base(BaseModel):
        port: int = 1

    class Narrowed(Base):
        port: Annotated[int, Field(ge=1000)] = 8080

    assert load(Narrowed, "[s]\nport = 9000\n").port == 9000

    with pytest.raises(InvalidError):
        load(Narrowed, "[s]\nport = 5\n")


# ── model_config ───────────────────────────────────────────────────────


def test_extra_forbid_refuses_a_key_the_model_does_not_declare(
    workspace: Path,
) -> None:
    class Forbidding(BaseModel):
        model_config = ConfigDict(extra="forbid")
        port: int = 1

    assert load(Forbidding, "[s]\nport = 2\n").port == 2

    with pytest.raises(InvalidError) as failure:
        load(Forbidding, "[s]\nport = 2\nstray = 3\n")

    assert any("stray" in str(error) for error in failure.value.errors)


def test_extra_allow_keeps_what_the_model_does_not_declare(
    workspace: Path,
) -> None:
    class Permissive(BaseModel):
        model_config = ConfigDict(extra="allow")
        port: int = 1

    loaded = load(Permissive, "[s]\nport = 2\nstray = 3\n")

    assert loaded.port == 2
    assert loaded.stray == 3


def test_a_frozen_model_loads_and_reloads(workspace: Path) -> None:
    """Frozen is about the instance; installing a new one is not mutation."""

    class Frozen(BaseModel):
        model_config = ConfigDict(frozen=True)
        port: int = 1

    Path("config.toml").write_text("[s]\nport = 2\n")

    config = DynamicConfig(Frozen, key="s").file("config.toml")
    config.init()

    assert config.current().port == 2

    Path("config.toml").write_text("[s]\nport = 3\n")
    config.reload()

    assert config.current().port == 3

    with pytest.raises(ValidationError):
        config.current().port = 4


def test_validate_assignment_does_not_disturb_loading(workspace: Path) -> None:
    class Checked(BaseModel):
        model_config = ConfigDict(validate_assignment=True)
        port: Annotated[int, Field(ge=1)] = 1

    assert load(Checked, "[s]\nport = 2\n").port == 2


# ── Aliases, in all four shapes ────────────────────────────────────────


def test_a_plain_alias_is_the_name_the_file_uses(workspace: Path) -> None:
    class Renamed(BaseModel):
        pool_size: int = Field(default=1, alias="poolSize")

    assert load(Renamed, "[s]\npoolSize = 8\n").pool_size == 8


def test_populate_by_name_accepts_both_spellings(workspace: Path) -> None:
    class Either(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        pool_size: int = Field(default=1, alias="poolSize")

    assert load(Either, "[s]\npoolSize = 8\n").pool_size == 8
    assert load(Either, "[s]\npool_size = 9\n").pool_size == 9


def test_alias_choices_accepts_any_of_them(workspace: Path) -> None:
    class Choosy(BaseModel):
        port: int = Field(
            default=1, validation_alias=AliasChoices("port", "listen_port")
        )

    assert load(Choosy, "[s]\nport = 2\n").port == 2
    assert load(Choosy, "[s]\nlisten_port = 3\n").port == 3


def test_an_alias_path_reads_from_nested_data(workspace: Path) -> None:
    class Nested(BaseModel):
        password: str = Field(
            default="", validation_alias=AliasPath("credentials", "password")
        )

    loaded = load(Nested, '[s]\n[s.credentials]\npassword = "pw"\n')

    assert loaded.password == "pw"


def test_an_alias_generator_renames_every_field(workspace: Path) -> None:
    def camel(name: str) -> str:
        head, *rest = name.split("_")

        return head + "".join(word.capitalize() for word in rest)

    class Generated(BaseModel):
        model_config = ConfigDict(alias_generator=camel, populate_by_name=True)
        pool_size: int = 1
        idle_timeout: int = 30

    loaded = load(Generated, "[s]\npoolSize = 8\nidleTimeout = 60\n")

    assert (loaded.pool_size, loaded.idle_timeout) == (8, 60)


def test_every_accepted_spelling_counts_as_known(workspace: Path) -> None:
    """An alias the model accepts is not an unknown key."""

    class Choosy(BaseModel):
        port: int = Field(
            default=1, validation_alias=AliasChoices("port", "listen_port")
        )

    Path("config.toml").write_text("[s]\nlisten_port = 3\n")

    config = DynamicConfig(Choosy, key="s").file("config.toml")
    report = config.check()

    assert report.is_clean, [unknown.path for unknown in report.unknown]


# ── Secrets under every alias shape ────────────────────────────────────
#
# The redaction list is derived from the model, so it has to know every
# name a file could use. Missing one is a secret in a diagnostic and in a
# "redacted" cache on disk — each of these was exactly that.


@pytest.mark.parametrize(
    ("field", "document", "path"),
    [
        (
            Field(
                default=SecretStr(""), validation_alias=AliasChoices("password", "pass")
            ),
            '[s]\npass = "hunter2"\n',
            "pass",
        ),
        (
            Field(default=SecretStr(""), alias="pw"),
            '[s]\npw = "hunter2"\n',
            "pw",
        ),
        (
            Field(
                default=SecretStr(""),
                validation_alias=AliasPath("credentials", "password"),
            ),
            '[s]\n[s.credentials]\npassword = "hunter2"\n',
            "credentials.password",
        ),
    ],
)
def test_a_secret_is_redacted_under_the_name_the_file_used(
    workspace: Path, field: object, document: str, path: str
) -> None:
    model = type(
        "Aliased",
        (BaseModel,),
        {
            "__annotations__": {"password": SecretStr},
            "password": field,
            "model_config": ConfigDict(populate_by_name=True),
        },
    )

    Path("config.toml").write_text(document)

    config = (
        DynamicConfig(model, key="s")
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert "hunter2" not in str(config.explain(path))
    assert "hunter2" not in Path("cache.json").read_text()


def test_a_secret_inside_a_pydantic_dataclass_is_redacted(workspace: Path) -> None:
    @pydantic_dataclass
    class Credentials:
        password: SecretStr = SecretStr("")

    class WithDataclass(BaseModel):
        credentials: Credentials = Field(default_factory=Credentials)

    assert secret_paths(WithDataclass) == ["credentials.password"]

    Path("config.toml").write_text('[s]\n[s.credentials]\npassword = "hunter2"\n')

    config = (
        DynamicConfig(WithDataclass, key="s")
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert "hunter2" not in str(config.explain("credentials.password"))
    assert "hunter2" not in Path("cache.json").read_text()
    assert config.current().credentials.password.get_secret_value() == "hunter2"


def test_a_secret_root_model_is_redacted_where_the_data_is(workspace: Path) -> None:
    class Token(RootModel[SecretStr]):
        pass

    class WithRoot(BaseModel):
        token: Token = Token(SecretStr(""))

    assert secret_paths(WithRoot) == ["token"], "not token.root, which no file writes"


def test_the_secret_list_covers_every_spelling_at_once(workspace: Path) -> None:
    class Aliased(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        password: SecretStr = Field(
            default=SecretStr(""),
            validation_alias=AliasChoices("password", "pass", "secret"),
        )

    assert set(secret_paths(Aliased)) == {"password", "pass", "secret"}


# ── Validators, computed fields, private state ─────────────────────────


def test_a_field_validator_rejects_before_anything_installs(
    workspace: Path,
) -> None:
    class Even(BaseModel):
        port: int = 2

        @field_validator("port")
        @classmethod
        def must_be_even(cls, value: int) -> int:
            if value % 2:
                raise ValueError("port must be even")

            return value

    Path("config.toml").write_text("[s]\nport = 4\n")

    config = DynamicConfig(Even, key="s").file("config.toml")
    config.init()

    Path("config.toml").write_text("[s]\nport = 5\n")

    with pytest.raises(InvalidError):
        config.reload()

    assert config.current().port == 4, "the rejected model never installed"


def test_a_model_validator_sees_the_whole_section(workspace: Path) -> None:
    class Ordered(BaseModel):
        low: int = 1
        high: int = 10

        @model_validator(mode="after")
        def in_order(self) -> Ordered:
            if self.low > self.high:
                raise ValueError("low must not exceed high")

            return self

    assert load(Ordered, "[s]\nlow = 2\nhigh = 8\n").low == 2

    with pytest.raises(InvalidError):
        load(Ordered, "[s]\nlow = 9\nhigh = 2\n")


def test_a_before_validator_reshapes_what_the_file_said(workspace: Path) -> None:
    class Splitting(BaseModel):
        hosts: list[str] = []

        @field_validator("hosts", mode="before")
        @classmethod
        def split(cls, value: object) -> object:
            return value.split(",") if isinstance(value, str) else value

    assert load(Splitting, '[s]\nhosts = "a,b,c"\n').hosts == ["a", "b", "c"]


def test_computed_fields_are_available_on_the_installed_model(
    workspace: Path,
) -> None:
    class Endpoint(BaseModel):
        host: str = "h"
        port: int = 1

        @computed_field  # type: ignore[prop-decorator]
        @property
        def url(self) -> str:
            return f"{self.host}:{self.port}"

    assert load(Endpoint, '[s]\nhost = "a"\nport = 2\n').url == "a:2"


def test_private_attributes_survive_a_load(workspace: Path) -> None:
    class WithState(BaseModel):
        port: int = 1
        _generation: int = PrivateAttr(default=7)

    assert load(WithState, "[s]\nport = 2\n")._generation == 7


# ── Structural shapes ──────────────────────────────────────────────────


def test_a_root_model_can_be_a_field(workspace: Path) -> None:
    class Ports(RootModel[list[int]]):
        pass

    class Listening(BaseModel):
        ports: Ports = Ports([1])

    assert load(Listening, "[s]\nports = [1, 2, 3]\n").ports.root == [1, 2, 3]


def test_a_pydantic_dataclass_can_be_a_field(workspace: Path) -> None:
    @pydantic_dataclass
    class Point:
        x: int = 0
        y: int = 0

    class Placed(BaseModel):
        point: Point = Field(default_factory=Point)

    loaded = load(Placed, "[s]\n[s.point]\nx = 1\ny = 2\n")

    assert (loaded.point.x, loaded.point.y) == (1, 2)


def test_a_generic_model_loads_both_ways(workspace: Path) -> None:
    T = TypeVar("T")

    class Box(BaseModel, Generic[T]):
        value: T

    class IntBox(Box[int]):
        pass

    assert load(IntBox, "[s]\nvalue = 5\n").value == 5
    assert load(Box[int], "[s]\nvalue = 6\n").value == 6


def test_a_discriminated_union_picks_the_right_member(workspace: Path) -> None:
    class Cat(BaseModel):
        kind: Literal["cat"] = "cat"
        lives: int = 9

    class Dog(BaseModel):
        kind: Literal["dog"] = "dog"
        tricks: int = 2

    class Pet(BaseModel):
        animal: Annotated[Union[Cat, Dog], Field(discriminator="kind")] = Cat()

    loaded = load(Pet, '[s]\n[s.animal]\nkind = "dog"\ntricks = 5\n')

    assert isinstance(loaded.animal, Dog)
    assert loaded.animal.tricks == 5


def test_optional_and_default_factory_fields(workspace: Path) -> None:
    class Sparse(BaseModel):
        maybe: Optional[int] = None
        tags: list[str] = Field(default_factory=lambda: ["default"])

    loaded = load(Sparse, "[s]\n")

    assert loaded.maybe is None
    assert loaded.tags == ["default"]


# ── pydantic-settings ──────────────────────────────────────────────────


def test_a_settings_class_is_a_schema_like_any_other(workspace: Path) -> None:
    class Simple(BaseSettings):
        host: str = "h"
        port: int = 1

    loaded = load(Simple, '[s]\nhost = "a"\nport = 2\n')

    assert (loaded.host, loaded.port) == ("a", 2)


def test_settings_inheritance_and_secrets_behave(workspace: Path) -> None:
    class Base(BaseSettings):
        host: str = "h"
        password: SecretStr = SecretStr("")

    class Extended(Base):
        port: int = 1

    Path("config.toml").write_text('[s]\nhost = "a"\nport = 2\npassword = "hunter2"\n')

    config = (
        DynamicConfig(Extended, key="s")
        .file("config.toml")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert config.current().port == 2
    assert "hunter2" not in Path("cache.json").read_text()
    assert "hunter2" not in str(config.explain("password"))


def test_a_settings_class_forbids_extra_keys_where_a_model_ignores_them(
    workspace: Path,
) -> None:
    """A difference in the *schema* half, and one that surprises people.

    `BaseSettings` sets `extra="forbid"` by default; `BaseModel` ignores
    what it does not declare. Pointing a narrow settings class at a
    section that carries more than it declares is a validation failure,
    not a shrug.
    """

    class AsSettings(BaseSettings):
        host: str = "h"

    class AsModel(BaseModel):
        host: str = "h"

    document = '[s]\nhost = "a"\nunrelated = 1\n'

    assert load(AsModel, document).host == "a"

    with pytest.raises(InvalidError):
        load(AsSettings, document)


def test_declared_sourcing_warns_rather_than_being_ignored(
    workspace: Path,
) -> None:
    """The footgun this exists to defuse: an env_prefix that does nothing."""

    class Declaring(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="DCTEST_")
        host: str = "h"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DynamicConfig(Declaring, key="s")

    assert len(caught) == 1
    assert "env_prefix" in str(caught[0].message)
    assert "from_settings" in str(caught[0].message)


def test_a_settings_class_without_sourcing_is_silent(workspace: Path) -> None:
    class Quiet(BaseSettings):
        host: str = "h"

    class Plain(BaseModel):
        host: str = "h"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DynamicConfig(Quiet, key="s")
        DynamicConfig(Plain, key="s")

    assert caught == []


def test_from_settings_translates_files_and_variables(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Pool(BaseModel):
        max_size: int = 1

    class Service(BaseSettings):
        model_config = SettingsConfigDict(
            env_prefix="DCTEST_",
            env_nested_delimiter="__",
            env_file=".env",
            toml_file="service.toml",
        )
        host: str = "h"
        port: int = 0
        pool: Pool = Pool()

    Path("service.toml").write_text(
        '[s]\nhost = "from-file"\nport = 1\n[s.pool]\nmax_size = 5\n'
    )
    Path(".env").write_text("DCTEST_PORT=2\nDCTEST_POOL__MAX_SIZE=6\n")

    config = DynamicConfig.from_settings(Service, key="s")
    config.init()

    loaded = config.current()
    assert loaded.host == "from-file", "the declared toml_file was read"
    assert loaded.port == 2, "the .env beat the file, under its own name"
    assert loaded.pool.max_size == 6, "nested names use the declared delimiter"

    # The real environment outranks the .env, as it does in pydantic-settings.
    monkeypatch.setenv("DCTEST_PORT", "3")
    config.reload()

    assert config.current().port == 3
    assert "DCTEST_PORT" in str(config.source_of("port"))


def test_from_settings_honours_case_sensitivity(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which spelling of the variable the binding looks for."""

    class Sensitive(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="dctest_", case_sensitive=True)
        port: int = 0

    class Insensitive(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="dctest_")
        port: int = 0

    Path("config.toml").write_text("[s]\nport = 1\n")
    monkeypatch.setenv("dctest_port", "11")
    monkeypatch.setenv("DCTEST_PORT", "12")

    sensitive = DynamicConfig.from_settings(Sensitive, key="s").file("config.toml")
    sensitive.init()

    insensitive = DynamicConfig.from_settings(Insensitive, key="s").file("config.toml")
    insensitive.init()

    assert sensitive.current().port == 11, "case_sensitive keeps the prefix as written"
    assert insensitive.current().port == 12, "the default upper-cases, as pydantic does"
    assert "dctest_port" in str(sensitive.source_of("port"))
    assert "DCTEST_PORT" in str(insensitive.source_of("port"))


def test_from_settings_refuses_what_it_cannot_translate(workspace: Path) -> None:
    """`secrets_dir` used to be here; the engine grew the source it needed.

    What is left are the two that have no engine equivalent and are not
    going to grow one: a command line belongs to the program, and a
    customised source order is one this cannot see, let alone reproduce.
    """

    class WithCli(BaseSettings):
        model_config = SettingsConfigDict(cli_parse_args=True)
        host: str = "h"

    with pytest.raises(ValueError, match="cli_parse_args"):
        DynamicConfig.from_settings(WithCli, key="s")

    class Customised(BaseSettings):
        host: str = "h"

        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):  # type: ignore[no-untyped-def]
            return ()

    with pytest.raises(ValueError, match="settings_customise_sources"):
        DynamicConfig.from_settings(Customised, key="s")

    class Plain(BaseModel):
        host: str = "h"

    with pytest.raises(TypeError, match="BaseSettings"):
        DynamicConfig.from_settings(Plain, key="s")


def test_from_settings_leaves_room_for_more_sources(workspace: Path) -> None:
    class Service(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="DCTEST_")
        host: str = "h"
        port: int = 0

    Path("extra.toml").write_text('[s]\nhost = "chained"\nport = 4\n')

    config = DynamicConfig.from_settings(Service, key="s").file("extra.toml")
    config.init()

    assert config.current().host == "chained"


def test_from_settings_binds_the_environment_without_a_prefix(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common shape: a settings class that declares no `env_prefix`.

    `BaseSettings` reads `HOST` and `PORT` in that configuration, so
    binding only when a prefix exists meant the environment reached
    nothing at all — silently, because an empty prefix is also not a
    declaration worth warning about.
    """

    class Bare(BaseSettings):
        host: str = "default"
        port: int = 1

    Path("config.toml").write_text('[s]\nhost = "from-file"\nport = 1\n')
    monkeypatch.setenv("HOST", "from-env")
    monkeypatch.setenv("PORT", "9999")

    config = DynamicConfig.from_settings(Bare, key="s").file("config.toml")
    config.init()

    assert config.current().port == 9999
    assert config.current().host == "from-env"
    assert "PORT" in str(config.source_of("port"))


def test_set_defaults_from_an_aliased_model_keeps_its_values(
    workspace: Path,
) -> None:
    """A model dumped by field name is a model the same class refuses.

    Without `by_alias`, `Aliased(VALUE=7)` round-tripped to `{"value": 7}`,
    which `model_validate` ignores — so the default vanished silently
    rather than raising.
    """

    class AliasedDefaults(BaseModel):
        value: int = Field(default=0, alias="VALUE")

    Path("config.toml").write_text("[s]\n")

    config = DynamicConfig(AliasedDefaults, key="s").file("config.toml")
    config.set_defaults(AliasedDefaults(VALUE=7))
    config.init()

    assert config.current().value == 7


def test_a_snapshot_keeps_a_large_integer_whole(workspace: Path) -> None:
    """The two public views of one snapshot have to agree.

    A `u64` above `i64::MAX` is an ordinary identifier. It reached the
    model exactly and the snapshot as a rounded float, which the book
    promises does not happen.
    """

    class Big(BaseModel):
        identifier: int = 0

    Path("config.json").write_text('{"s": {"identifier": 18446744073709551615}}')

    config = DynamicConfig(Big, key="s").file("config.json")
    config.init()

    exported = config.snapshot().to_dict()["identifier"]

    assert config.current().identifier == 18446744073709551615
    assert exported == config.current().identifier
    assert isinstance(exported, int), "a float here has already dropped digits"


def test_from_settings_translates_a_secrets_directory(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one translation `from_settings` used to refuse.

    Docker and Kubernetes mount secrets as a directory of single-value
    files; pydantic-settings reads it as `secrets_dir`, and the engine
    had no such source until it grew one.
    """

    class Mounted(BaseSettings):
        model_config = SettingsConfigDict(secrets_dir="secrets")
        host: str = "default"
        password: str = ""

    Path("secrets").mkdir()
    Path("secrets/password").write_text("hunter2\n")
    Path("config.toml").write_text('[s]\nhost = "from-file"\n')

    config = DynamicConfig.from_settings(Mounted, key="s").file("config.toml")
    config.init()

    assert config.current().password == "hunter2", "one trailing newline trimmed"
    assert config.current().host == "from-file"
    assert "secrets/password" in str(config.source_of("password")), (
        "provenance names the individual file, which is what makes this "
        "better than a store that says only `secrets_dir`"
    )


def test_a_mounted_secret_is_redacted_like_any_other(workspace: Path) -> None:
    """The derivation is by path, so this should be free — pinned anyway."""

    class Mounted(BaseModel):
        password: SecretStr = SecretStr("")

    Path("secrets").mkdir()
    Path("secrets/password").write_text("hunter2\n")

    config = (
        DynamicConfig(Mounted, key="s")
        .secrets_dir("secrets")
        .cache("cache.json", "redacted")
    )
    config.init()

    assert config.current().password.get_secret_value() == "hunter2"
    assert "hunter2" not in str(config.explain("password"))
    assert "hunter2" not in Path("cache.json").read_text()
