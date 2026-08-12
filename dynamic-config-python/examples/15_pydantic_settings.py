"""An existing pydantic-settings class, with hot reload underneath it.

    pip install pydantic-settings
    python examples/15_pydantic_settings.py

`BaseSettings` is two things bolted together: a Pydantic model, and a set
of places to read it from. The model half is welcome here unchanged — it
is a `BaseModel`, and every Pydantic feature works. The sourcing half is
what this engine does instead, and better: layering, precedence you can
explain, a last-known-good cache, and reloads without a restart.

The catch is that pydantic-settings' sourcing does not run under
`model_validate`, which is how this binding validates. So a settings
class whose `SettingsConfigDict` declares `env_prefix` would quietly get
none of it. Two answers, both shown below:

  `DynamicConfig(Settings, ...)`             warns, and is the source
  `DynamicConfig.from_settings(Settings, …)` translates the declaration
                                             into engine sources
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr

from _shared import show
from dynamic_config import DynamicConfig

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover - the example says how to fix it
    raise SystemExit(
        "this example needs pydantic-settings: pip install pydantic-settings"
    ) from None


class Pool(BaseModel):
    """A nested section, to show what the nested delimiter reaches."""

    max_size: int = Field(default=8, ge=1, le=1000)


class ServiceSettings(BaseSettings):
    """The class a project already has, unchanged.

    `env_prefix` and `env_file` are the declaration a deployment already
    satisfies: `APP_PORT` is set in the environment, `.env` is on disk.
    Neither name changes by moving to this engine.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=".env",
        toml_file="service.toml",
    )

    host: str = "localhost"
    port: int = 8080
    password: SecretStr = SecretStr("")
    pool: Pool = Pool()


def main() -> None:
    """Runs the pydantic-settings example end to end."""
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)

        Path("service.toml").write_text(
            '[svc]\nhost = "db.internal"\nport = 8080\n'
            'password = "hunter2"\n\n[svc.pool]\nmax_size = 16\n'
        )
        Path(".env").write_text("APP_PORT=9090\nAPP_POOL__MAX_SIZE=32\n")

        show("from_settings: the declaration becomes engine sources")
        config = DynamicConfig.from_settings(ServiceSettings, key="svc")
        config.init()

        loaded = config.current()
        print(f"  host          → {loaded.host}   (the declared toml_file)")
        print(f"  port          → {loaded.port}   (APP_PORT, from the .env)")
        print(f"  pool.max_size → {loaded.pool.max_size}   (APP_POOL__MAX_SIZE)")
        print("  the variable names are the ones the class already declared")

        show("and the real environment outranks the .env, as it always did")
        os.environ["APP_PORT"] = "7070"
        config.reload()
        print(f"  port        → {config.current().port}")
        print(f"  source_of() → {config.source_of('port')}")
        del os.environ["APP_PORT"]

        show("provenance, which pydantic-settings has no answer for")
        print("  " + str(config.explain("pool.max_size")).replace("\n", "\n  "))

        show("secrets are still derived from the model")
        print(f"  password → {config.current().password}")
        print("  " + str(config.explain("password")).replace("\n", "\n  "))
        print("  a redacted cache carries the shape, not the value")

        show("declaring sources and not using from_settings is a warning")
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            DynamicConfig(ServiceSettings, key="svc")

        print(f"  {caught[0].category.__name__}: {str(caught[0].message)[:96]}…")
        print("  silence would be the bug: an env_prefix that reads nothing")

        show("what cannot be translated is refused, not dropped")

        class WithSecretsDir(BaseSettings):
            """A source with no engine equivalent — a directory of values."""

            model_config = SettingsConfigDict(secrets_dir="/run/secrets")
            host: str = "localhost"

        try:
            DynamicConfig.from_settings(WithSecretsDir, key="svc")
        except ValueError as failure:
            print(f"  refused: {failure}")

        show("a settings class that declares nothing is just a model")

        class Plain(BaseSettings):
            """No sourcing declared: the configuration says where to read."""

            host: str = "localhost"
            port: int = 1

        # Its own file, because a `BaseSettings` defaults to
        # `extra="forbid"` where a `BaseModel` ignores what it does not
        # declare — a section carrying keys this class has never heard of
        # is a validation failure, not a shrug. Worth knowing before you
        # point a narrow settings class at a wide section.
        Path("plain.toml").write_text('[svc]\nhost = "plain.internal"\nport = 3000\n')

        plain = DynamicConfig(Plain, key="svc").file("plain.toml").env("APP_")
        plain.init()
        print(
            f"  {plain.current().host}:{plain.current().port} — no warning, no surprise"
        )


if __name__ == "__main__":
    main()
