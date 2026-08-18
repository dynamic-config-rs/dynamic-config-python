# The Decorator & Typing

## The decorator

```python
from dynamic_config import dynamic_config

@dynamic_config(key="db", files=["config.toml"], env="APP_")
class Database(BaseModel):
    host: str
    port: int = 5432

Database.config.init()
Database.current()
```

Sugar over the same object: the decorator builds a `DynamicConfig`,
stores it as `Model.config` and attaches `current`/`try_current`/
`reload`/`source_of`/`explain` classmethods. It does **not** load at
import time — reading files while a module is being imported is a
surprise nobody asked for; pass `init=True` when a script wants exactly
that. Decorating one class twice is an error, mirroring the crate's
one-configuration-per-type rule.

In Python a runtime-configured decorator is idiomatic where Rust's
argument-free attribute was not. The engine-level rule holds in both:
declaration is separate from the configurable builder underneath.

### Inherit `Configured` if you type-check

The decorator attaches its six members at runtime, and **no type checker
can see that**: Python has no way to spell "this class, plus these
members", so `Database.current()` is an `attr-defined` error under
`mypy --strict` and nothing at all to an editor's completion.

```python
from dynamic_config import Configured, dynamic_config

@dynamic_config(key="db", files=["config.toml"])
class Database(Configured, BaseModel):
    host: str = "localhost"

Database.current().host      # `str`, and it completes
```

`Configured` declares the members where a checker sees them; the
decorator fills them in. It adds no fields, so `model_fields` is
unchanged, and the runtime behaviour is identical either way.

**`Model.config` keeps the model's type too**, which matters as soon as
you reach past the six classmethods for the async surface:

```python
await Database.config.init_async()

async for database in Database.config.changes():
    database.host                # `str`, not `Any`
```

That is a descriptor rather than an annotation, because a `ClassVar`
cannot carry a type variable — the same device `classmethod` uses in
typeshed, and the reason `Database.config` is `DynamicConfig[Database]`
rather than `DynamicConfig[Any]`.

The decorator on its own still works and is not deprecated — but it
cannot be made visible to a checker, and `tests/typing/usage.py` in the
repository is where that promise is kept: `mypy --strict` runs over a
file written the way a caller writes one, because types that regress for
a user are invisible to a test suite.

## Typing

`DynamicConfig` is `Generic[M]`, so `current()` comes back as *your*
model rather than as `Any`:

```python
config = DynamicConfig(Database, key="db")
reveal_type(config.current())          # Database
```

Stubs ship with the package and `mypy --strict` runs over the facade in
CI, so this stays true.

## With a web framework

```python
@app.get("/health")
async def health() -> dict[str, str]:
    db = config.current()       # once per request; reuse the value
    return {"host": db.host}
```

Read `current()` once per request and use that value, exactly as the Rust
guidance says: a reload landing mid-request would otherwise show one
request two configurations. [Web Frameworks](frameworks.md) has
the FastAPI, Flask and Django patterns in full, including what not to do
in each (copying into `app.config`, freezing into Django's settings), and
the pre-forking-server caveats.

## Data types

Whatever Pydantic validates, this loads: enums, `datetime`, `UUID`,
`Decimal`, paths, addresses, containers, nested models, unions,
`SecretStr`. [Data Types](types.md) covers the whole range and the
three conversions that have to be exact — an integer staying an integer,
a bool not becoming `1`, and a large `u64` keeping its digits.

A `msgspec.Struct` is the fifth kind of declaration, and the fastest:
msgspec builds the instance in C, declares a secret through
`Meta(extra={"secret": True})`, and leaves unknown keys to the struct's
own `forbid_unknown_fields`. [A msgspec
Struct](types.md#a-msgspec-struct-and-what-it-answers-differently)
has the three answers that are msgspec's rather than this library's.

And whatever a *model* may be, it may be the schema: inheritance,
mixins, `model_config`, validators, computed fields, `RootModel`,
Pydantic dataclasses, generics, discriminated unions — and
`pydantic_settings.BaseSettings`, whose own sourcing declaration
`DynamicConfig.from_settings(...)` translates into engine sources so an
existing settings class keeps the variable names its deployment already
sets. [What a schema may be](types.md#what-a-schema-may-be) and
[pydantic-settings](types.md#pydantic-settings).

## What is not exposed

| Not exposed | Why |
|---|---|
| The [store crates](remote-stores.md) themselves | their clients — gRPC, the AWS SDK, three HTTP stacks — would ride into every wheel |
| Encrypted files | decryption needs a `Decryptor`, which is a Rust trait; decrypt with the [CLI](https://dynamic-config-rs.github.io/cli.html) and point this at the result |
| `save`, JSON Schema | Pydantic already does both, better |

The *door* those crates go through is exposed: `RemoteSource` is
implementable in Python, so a store with no Rust client is a class with
`fetch()` and `describe()` — see [Remote Stores in
Python](remote-stores.md). The first row is on the roadmap as an
opt-in wheel; the last two are not.
[Limitations](limitations.md) has the full list with the reasoning
— including the constraints that are not omissions at all: sources fixed
after the first load, one watcher per configuration, and why
`ValidationError` is rebuilt rather than re-raised.

## Examples

Twenty-seven runnable scripts ship with the package —
[`examples/`](https://github.com/dynamic-config-rs/dynamic-config-python/tree/main/dynamic-config-python/examples),
from the twenty-line quick start to multi-tenant configuration, the
diagnostics tour, several configurations on one event loop (as values
and as decorated classes), every callback shape — synchronous and
dispatched — a group of configurations reloaded atomically, the event
stream, an existing `pydantic-settings` class, a remote store written in
Python with a synchronous client and one with an async client, and the
three framework integrations. None needs a server or a setup step,
and all of them run in CI, because an example nobody runs is
documentation that has already started rotting — and the framework ones
are driven again by the integration suite, which asserts what they
answer rather than only that they exit zero.

## How it is built

[Implementation Details](internals.md) is the inside view: where
validation is hooked and why that placement is the whole design, how a
validated model is published exactly once, why the read path never
crosses back into Rust, what the GIL and thread rules are, and what the
two changes the Rust crate needed were.

## Interpreter shutdown

A watcher thread that outlives finalization would call into a Python that
is no longer there — the classic embedding crash. The binding registers
an `atexit` handler that stops every watcher and drops every cached model
while the interpreter is still whole, and the test suite exercises exactly
that: a process exiting mid-reload-storm with a detached watcher running.
