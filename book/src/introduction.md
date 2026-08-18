# Python Bindings

`dynamic-config-py` pairs this engine with the schema you already write:
**Rust resolves, your schema validates, Python reads a cache.**

```sh
pip install dynamic-config-py                     # the import is `dynamic_config`
pip install dynamic-config-py[pydantic]           # + Pydantic models
pip install dynamic-config-py[pydantic-settings]  # + BaseSettings classes
pip install dynamic-config-py[msgspec]            # + msgspec Structs
pip install dynamic-config-py[all]                # the Pydantic pair
```

The base install has **no dependencies**: the engine is compiled into the
wheel, and a `dataclasses.dataclass` is a schema here. Pydantic and
msgspec are extras because each is a choice — see
[What a schema may be](types.md#what-a-schema-may-be) for what
each kind validates, including `Values`, which is
[no schema at all](types.md#values-a-configuration-with-no-schema):
a configuration read by dotted path, for the keys a program learns at run
time.

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

Everything the Rust side does with sources happens here unchanged: files
merge in call order, the environment beats them, `.env` sits just below
the real environment, profiles overlay sibling files, discovery sits
below listed files, and the runtime layers bracket the rest. The
[precedence chapter](https://dynamic-config-rs.github.io/sources-and-precedence.html) is the contract for both
languages.

## Where validation happens

```text
reload trigger (watcher / reload() / init())
    → Rust: load, merge, strict checks          no GIL
    → Rust: resolved tree → dict                GIL, microseconds
    → Python: Model.model_validate(dict)        GIL, once per reload
    → ok:  swap the cached model, wake readers, run hooks
    → err: nothing installs — the previous model keeps serving
```

Validation is the engine's own `validate` hook, which the loader calls
**before** it installs anything. That placement is the whole design:

- **A reader never pays.** `current()` returns the cached instance —
  no boundary crossing, no validation, no lock a writer holds.
- **A reload Pydantic rejects changes nothing.** The previous model keeps
  serving, the generation does not move, and the last-known-good cache is
  not written — exactly what a Rust `validate` refusal does.
- **Validation runs once per resolve**, not once per read and not twice
  per reload.

## Where everything else is

- [Quick Start](quick-start.md) — five minutes to a reloading config.
- [Core Concepts](concepts.md) — the lifecycle, testing, secrets, what a
  read costs, diagnostics and errors.
- [The Decorator & Typing](decorator-and-types.md) — `@dynamic_config`,
  `Configured`, and the shapes a schema can take.
- [Web Frameworks](frameworks.md) — the request-scoped story, and
  [`dynamic-config-py-web`](https://dynamic-config-rs.github.io/web/)
  when you want it installed rather than described.
