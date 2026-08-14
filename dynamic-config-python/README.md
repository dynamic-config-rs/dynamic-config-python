# dynamic-config-py

Hot-reloadable configuration for Python: **Rust resolves, your schema
validates**.

```sh
pip install dynamic-config-py                     # dataclasses; no dependencies
pip install dynamic-config-py[pydantic]           # + Pydantic models
pip install dynamic-config-py[pydantic-settings]  # + BaseSettings classes
pip install dynamic-config-py[msgspec]            # + msgspec Structs
pip install dynamic-config-py[all]                # the Pydantic pair
pip install dynamic-config-py[remote]             # + the Rust etcd and Vault clients
```

`[all]` is the Pydantic extras — a few hundred kilobytes of pure Python.
msgspec is a different validation engine rather than an addition to that
one, so it is its own extra and not in `[all]`.
`[remote]` is a **second wheel**, because a gRPC stack in the ordinary one
would be in every install; it is not in `[all]` for that reason.

```python
from dataclasses import dataclass
from dynamic_config import DynamicConfig

@dataclass
class Database:
    host: str = "localhost"
    port: int = 5432

db = (
    DynamicConfig(Database, key="db")
    .file("config.toml")
    .env("APP_")
    .init_and_current()    # a Database instance — cached, not re-validated
)
```

The schema can be a `dataclasses.dataclass`, a Pydantic model, a Pydantic
dataclass, a `BaseSettings` class or a `msgspec.Struct` — or `Values`,
which is no schema at all: a configuration read by dotted path, for the keys a program learns
at run time rather than declares. Everything else — sources, precedence,
watching, recovery, diagnostics — is the same object whichever it is;
what changes is what validation means and what you install.

The engine is the [`dynamic-config`] Rust crate: files, environment
layering, `.env`, profiles, discovery, precedence, a debounced file
watcher, last-known-good recovery and provenance. A dataclass schema is
validated structurally — required fields, unknown keys, nested
dataclasses, declared types. A Pydantic one is validated by Pydantic, all
of it: `field_validator`, `model_validator`, aliases, `SecretStr`. A
msgspec one is validated in C, with its own `Meta` constraints and a
secret declared as `Meta(extra={"secret": True})`.

**Validation runs once per successful resolve, never per read.**
`current()` returns a cached instance, so reading configuration on every
request costs an attribute lookup rather than a boundary crossing.

## Python versions

| Line | Wheel | Tested |
|---|---|---|
| 3.9 – 3.14 | one abi3 wheel per platform | every commit, every line |
| 3.14t (free-threaded) | its own `cp314t` wheel | every commit, concurrency suite ten times over |
| 3.8 and older | — | not supported; `requires-python` refuses |

Linux (manylinux 2_28) x86-64 and aarch64, macOS x86-64 and arm64, Windows
x86-64. Raising the floor is treated as a breaking change and will not
happen before 1.0. The full table, and what each row is tested with, is in
[Stability & Production Use](https://ctolon.github.io/dynamic-config/python/stability.html).

## What it gives you

```python
config.init()                      # load, validate, install
config.init_and_current()          # …and hand back the model, in one line
config.reload()                    # again, on demand
watch = config.watch(debounce=0.25)  # and again on every file change

config.current()                   # the model, cached
config.try_current()               # or None, before the first load

@config.on_change("pool_size")       # only when that path moved
def resize(old, new):
    pool.resize(new.pool_size)
```

Every blocking call has an async twin that runs the work off the loop —
`init_async`, `load_async`, `reload_async` — plus two ways to wait:

```python
await config.init_async()

model = await config.changed_async(timeout=30)   # the next install, once

async for db in config.changes():                # every install, forever
    await pool.resize(db.pool_size)
```

Cancelling either wait is noticed within a quarter second, and leaves the
engine untouched. Which thread pool pays for the blocking half is yours
to choose — `dynamic_config.set_executor(pool)` process-wide, or
`DynamicConfig(..., executor=pool)` for one configuration — the same
question the Rust crate's `set_blocking_executor` answers.

A reload that Pydantic rejects **keeps the previous model serving** —
exactly as a bad file edit does. Nothing installs, the last-known-good
cache is not written, and the error is reported rather than raised at a
reader.

### Diagnostics that answer the actual question

```python
config.source_of("port")     # Origin(kind='env', detail='APP_DB_PORT')
config.is_set("pool.size")   # False
print(config.explain("port"))  # every layer's answer, as a table
config.check()               # would it load? any unknown keys?
config.snapshot().to_dict()  # the resolved section, as data
```

`explain` is the one diagnostic that prints values, and it redacts:
fields typed `SecretStr` or `SecretBytes` read `***`. **Nobody
re-declares which fields are secret** — the binding derives the list from
the model's own types, nested models included, and the redacted cache and
the scrubbed validation errors follow from the same list.

### Testing, with the cleanup written down

```python
with config.overrides(pool_size=1, host="localhost"):
    ...        # reloaded on entry; the previous overrides are back on exit
```

The exit restores the override layer the block *found* rather than
emptying it, so a nested `with` composes and a pin set before the block
survives it — and it restores on an exception too, so a failing assertion
does not decide what the next test sees. Dotted paths are spelled with
`__`, as in the environment layer: `pool__max_size=1`.

The filesystem and environment half ships as a pytest plugin, found
through a `pytest11` entry point — installing the package is the whole
setup:

```python
def test_the_service_reads_its_file(dynamic_config_workspace):
    (dynamic_config_workspace / "app.toml").write_text('[db]\nport = 5432\n')
    config = DynamicConfig(Database, key="db").file("app.toml")

    assert config.init_and_current().port == 5432
```

`dynamic_config_env("APP_")` is the other fixture: it unsets the
variables a developer's shell would otherwise contribute. Neither is
autouse, and `dynamic_config.pytest` imports pytest and the standard
library and nothing else — it is loaded in every pytest run of every
environment this package is installed in.

### The decorator, for the settings crowd

```python
from dynamic_config import Configured, dynamic_config

@dynamic_config(key="db", files=["config.toml"], env="APP_")
class Database(Configured, BaseModel):
    host: str
    port: int = 5432

Database.config.init()
Database.current().host      # typed as `str`, and it completes in an editor
```

`Configured` is what makes the attached members visible to a type checker
and to an editor — attributes attached at runtime are invisible to both.
The decorator works without it; the completion does not.

It does not load at import time — reading files while a module is being
imported is a surprise nobody asked for. `init=True` says otherwise.

## The rules it keeps

- **A reader never pays for a reload.** No per-read validation, no
  per-read boundary crossing, no lock a writer can hold.
- **A bad reload changes nothing.** The previous model keeps serving; the
  failure is reported where it happened.
- **Values stay out of diagnostics.** Every `repr` here shows shape, not
  values; `explain` is the documented exception, and it redacts secrets.
  Pydantic's `ValidationError` normally echoes the offending input — at
  this boundary it is scrubbed to locations, messages and error types,
  attached as `error.errors`.
- **Interpreter shutdown is not a crash.** Watcher threads are stopped
  before finalization, so nothing calls into a Python that is no longer
  there.

## Not exposed, deliberately

- **The remote store crates** (etcd, Consul, Vault, NATS, Redis, S3,
  Firestore). Their clients would ride into every wheel; they stay in
  Rust until there is a reason to pay that. The *door* they go through
  is here — see [A store of your own](#a-store-of-your-own).
- **Encrypted files.** Decryption needs a `Decryptor` implementation,
  which is a Rust trait; a deployment that needs it decrypts with the CLI
  and points this at the result.
- **`save` and JSON Schema.** Pydantic already does both, better.
- **A `pydantic-settings` source shim.** Wiring in as a
  `PydanticBaseSettingsSource` would inherit that library's lifecycle —
  read once, at construction — and lose the reloading that is the point.
  Support goes the other way instead:
  [`DynamicConfig.from_settings`](#pydantic-settings) turns a settings
  class's own declaration into engine sources.

## A store of your own

A remote store is an object with `fetch()` and `describe()`, so a
company's own service — or anything nobody will write a Rust client for
— needs no Rust:

```python
from dynamic_config import DynamicConfig, Format, RemoteSource

class ConfigService(RemoteSource):
    def fetch(self):
        return httpx.get(URL, timeout=5).text, Format.JSON

    def describe(self):
        return "the config service"

config = DynamicConfig(Database, key="db").remote(ConfigService())
config.refresh_remote()      # reads the store, keeps the document
config.init()                # merges it — above the files, below the environment
```

Fetching is explicit, exactly as it is in Rust: a load merges what was
last fetched and touches no network. A `fetch()` that raises arrives as
`RemoteError` — or `AuthError`, if that is what it raised — with the
original attached as `__cause__` and its message deliberately not
repeated, because a store's exception routinely carries the URL it
called. Nothing is poisoned: the previous document and the previous model
both keep serving.

The GIL is not held across the fetch — a `fetch()` doing I/O releases it
the way any Python thread does, measured at 68–102% of a second thread's
free-running rate — and a `fetch()` may read the configuration it is
fetching for. [Remote Stores in
Python](https://ctolon.github.io/dynamic-config/python/remote-stores.html)
is the whole story.

## pydantic-settings

A `BaseSettings` class is a `BaseModel`, so it works here as a schema
unchanged. What does *not* carry over is its sourcing: pydantic-settings
reads its sources in `__init__`, and this binding validates with
`model_validate`, which does not go through it. A class declaring
`env_prefix` would therefore get none of it — silently, which is the part
worth fixing.

```python
config = DynamicConfig.from_settings(ServiceSettings, key="svc")
config.init()
```

`from_settings` reads the class's `SettingsConfigDict` and rebuilds it as
engine sources: `toml_file`/`json_file`/`yaml_file` become files,
`env_file` becomes the dotenv layer, and `env_prefix` becomes one binding
per leaf field — so `APP_PORT` stays `APP_PORT` rather than becoming
`APP_<KEY>_PORT`, and a deployment's existing variables keep working.
`env_nested_delimiter` and `case_sensitive` shape those names.

What has no engine equivalent is refused at the call rather than dropped:
`secrets_dir`, `cli_parse_args`, and an overridden
`settings_customise_sources`. Using `DynamicConfig(...)` directly on a
class that declares sourcing warns and carries on — the configuration is
the source there, which is a fine thing to want, as long as nobody
believes the `env_prefix` is doing something.

One difference in the schema half is worth knowing: `BaseSettings`
defaults to `extra="forbid"` where `BaseModel` ignores what it does not
declare, so a narrow settings class pointed at a wide section fails
validation rather than shrugging.

## Examples

Eighteen runnable scripts in
[`examples/`](https://github.com/ctolon/dynamic-config/tree/main/dynamic-config-python/examples) — the quick start,
layering and precedence, watching, asyncio (single- and multi-file), the
decorator (plain, and several configurations on one event loop),
multi-tenant configuration, secrets and recovery, the diagnostics tour,
test overrides, every callback shape, pydantic-settings, a remote store
written in Python, and FastAPI, Flask and Django integrations. All of
them run in CI.

```sh
python examples/01_quick_start.py
```

## How it works

[Implementation Details](https://ctolon.github.io/dynamic-config/python/internals.html)
covers the inside: validation hooked *before* the install (which is what
makes a rejected reload change nothing), the sequence number that
publishes each model exactly once, the Python-side cache that keeps a
read at 28 ns, the GIL and thread rules, and interpreter-shutdown safety.

## Requirements

Python 3.9+ (abi3 wheels), Pydantic 2. The distribution is
`dynamic-config-py`; the import is `dynamic_config`.

**Free-threaded CPython 3.14t is supported on Linux.** A `Py_GIL_DISABLED`
build has no stable ABI, so it gets a `cp314t` manylinux wheel of its own
rather than riding the abi3 one, and the module declares
`Py_mod_gil = Py_MOD_GIL_NOT_USED` so the interpreter does not turn the GIL
back on for the process at import. 3.14t and not 3.13t: PyO3 dropped 3.13t
when CPython promoted free-threading from experimental to supported. The
audit behind the declaration — and what a green suite still does not prove
— is
[Free-Threaded CPython](https://ctolon.github.io/dynamic-config/python/free-threading.html).

## License

MIT

[`dynamic-config`]: https://github.com/ctolon/dynamic-config
