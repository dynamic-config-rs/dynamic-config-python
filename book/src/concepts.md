# Core Concepts

## The lifecycle

```python
config.init()                          # load, validate, install
candidate = config.load()              # validate only — installs nothing
config.reload()                        # again, on demand

watch = config.watch(debounce=0.25)    # and on every file change
watch.stop()                           # or use it as a context manager

config.current()                       # the model; raises before the first load
config.try_current()                   # or None
config.replace(Database(host="x"))     # install one you built

with config.running():                 # or the whole lifetime as one block
    serve()
```

Every call that touches the sources has an async twin —
`init_async`, `load_async`, `reload_async`, `changed_async` — and
[API Reference](reference.md) is the full list, with each pair on
one row.

`current()` has none: the model is cached on the
configuration object, so reading it is an attribute lookup that needs no
`await` on a loop and blocks nothing on a thread.

`watch(poll_interval=...)` chooses polling over the platform's
notification backend — what network and overlay filesystems need, where
the native watch registers successfully and then silently never fires.
One watcher per configuration object; two configurations of the same
model watch side by side.

### Reacting to a reload

```python
@config.on_change("pool_size")         # only when that path moved
def resize(old, new):
    pool.resize(new.pool_size)

@config.on_reload_async                # or as a task on this event loop
async def reconnect(old, new):
    await pool.resize(new.pool_size)

async for db in config.changes():      # any event loop, no callback
    pool.resize(db.pool_size)
```

By default hooks run on whichever thread performed the reload, so keep
them short: compare, then *signal* the subsystem that owns the resource
— the
[reload lifecycle](https://dynamic-config-rs.github.io/reload-lifecycle.html) chapter is the same argument in
Rust. A hook that raises is reported through Python's unraisable channel
and the remaining hooks still run.

…or say where the hook should run instead: `dispatch=` moves it to the
configuration executor or onto the loop that registered it, and
`backpressure=` says what to do when installs arrive faster than it
finishes. [Callbacks](callbacks.md) is the whole surface: what `old` and
`new` mean, why a read inside a hook already sees the new model, the
filter above, the scoped `with config.on_reload(...)` form, and the two
tables in full.

`changes()` is an async iterator resolved by a notifier thread parked in
the engine with the GIL released, so it drives on asyncio, uvloop, trio's
asyncio compatibility layer — anything — and cancelling it is immediate
rather than polled. There is a blocking `changed(timeout=…)` for threads,
an awaitable `changed_async(timeout=…)` for a single shot, and
`events()` for the diagnostic stream of installs and refusals.
[Async & asyncio](async.md) is the whole story: which calls block, which
thread each piece runs on, and how cancellation behaves.

### Several configurations at once

```python
group = ConfigGroup(database, cache, queue)

with group.running():                  # init all, watch all, stop all
    serve()

group.reload_atomic()                  # every member validates, or none installs
```

The group owns lifecycle, not storage — `database.current()` is still the
read path. [Async & asyncio](async.md#several-configurations-one-lifecycle)
has the rest, including why `reload_atomic` exists.

## Testing

The most common shape this library is used in is a test that wants one
value pinned:

```python
with config.overrides(pool_size=1, host="localhost"):
    assert under_test(config) == "localhost, one connection"
```

Reloaded on entry, and on exit the *previous* override layer is restored
and reloaded — restored rather than emptied, so a nested `with` composes
and a pin made before the block still stands after it. The restore runs
on an exception too: the long hand is
`set_override`, `reload`, `clear_overrides`, `reload`, and it is the last
two that get forgotten, after which one test's pin is the next test's
mystery. Dotted paths are spelled with `__` — `pool__max_size=1` — the
same nesting rule the environment layer uses.

The other half of an isolated configuration test is the filesystem and
the environment, and the package ships those as a pytest plugin. It
loads through a `pytest11` entry point, so installing the package is the
whole setup:

```python
def test_the_service_reads_its_file(dynamic_config_workspace):
    (dynamic_config_workspace / "app.toml").write_text('[db]\nport = 5432\n')
    config = DynamicConfig(Database, key="db").file("app.toml")

    assert config.init_and_current().port == 5432
```

`dynamic_config_workspace` is a temporary directory that is also the
working directory, so a relative `file("app.toml")` finds this test's
copy; `dynamic_config_env("APP_")` unsets the variables a developer's
shell would otherwise contribute. Neither is autouse, and the module
imports pytest and the standard library only — it is loaded in every
pytest run of every environment this package is installed in, so a
dependency there would be a dependency for all of them.
[The reference](reference.md#testing) has both fixtures, and the
`conftest.py` that makes the environment one automatic.

## Secrets are derived, not re-declared

```python
class Database(BaseModel):
    host: str
    password: SecretStr        # this is the declaration
```

At construction the binding walks `model_fields` for `SecretStr` and
`SecretBytes` — through `Optional`, unions, containers, nested models,
Pydantic dataclasses and `RootModel`, as dotted paths — and seeds the
same secret list the generated Rust `builder()` seeds. A field is listed
under **every** name a file could carry it under, its aliases included,
because a secret spelled the other way is still a secret
([Aliases](types.md#aliases-in-all-four-shapes)). Everything
downstream follows from it: the redacted
[last-known-good cache](https://dynamic-config-rs.github.io/persistence.html#last-known-good) drops those
fields, `explain` prints them as `***`, and Pydantic's `ValidationError`
— which by default echoes the offending input — is scrubbed to
locations, messages and error types before it crosses the boundary.

Nobody keeps a second list in step with the first, because there is no
second list.

## What a read costs

The claim is that reading configuration is an attribute lookup. Measured
with `python benchmarks/read_path.py`, which prints the machine it ran on
above the numbers — because a nanosecond figure without one is not a
measurement:

```text
  cpu         Intel(R) Core(TM) i7-14700F
  cores       28
  memory      126 GiB
  os          Linux 7.1.4
  python      CPython 3.14.6 (release)
  rounds      200000 per measurement
```

| | ns per read | against a global |
|---|---|---|
| `config.current()` | 29 | 1.1× |
| a module global | 27 | — |
| `Model.model_validate(dict)` | 990 | 34× |

`current()` is within a tenth of a bare global because it *is* a Python
attribute: the engine publishes each new model into the configuration
object as it installs, so a read never crosses back into Rust. The third
row is the number that matters — it is what every read would cost if the
model were validated per read instead of once per reload.

The **ratios** are what travels between machines; the nanoseconds belong
to that block above them. A slower laptop moves all three numbers and
leaves the two ratios where they are, which is why the argument is made
with ratios.

## Diagnostics

```python
config.source_of("port")       # Origin(kind='env', detail='APP_DB_PORT')
config.is_set("pool.size")     # False
print(config.explain("port"))  # every layer's answer, as a table
report = config.check()        # would it load? any unknown keys?
config.snapshot().to_dict()    # the resolved section, as data
```

The rules the [diagnostics chapter](https://dynamic-config-rs.github.io/validation-diagnostics.html) states hold
here too: paths, never values — except `explain`, which is the one
diagnostic whose job is values, and which redacts the secret ones. Every
`repr()` in the binding shows shape rather than content, so an object
landing in a log line cannot leak a configuration.

`changed_paths` is the audit half of a reload — what *moved*, without
what it moved to:

```python
from dynamic_config import changed_paths

config.on_reload(
    lambda old, new: log.info("configuration changed: %s",
                              ", ".join(str(c) for c in changed_paths(old, new)))
)
```

It compares the real values — including secrets, because comparing the
mask Pydantic renders would make two different passwords look identical
and miss the one change most worth noticing — and reports only paths and
whether each was added, removed or changed.

## Errors

One hierarchy, mirroring `ErrorKind`:

```python
from dynamic_config import DynamicConfigError, InvalidError, MissingError

try:
    config.init()
except InvalidError as error:
    for report in error.errors:       # Pydantic's own report, scrubbed
        print(report["loc"], report["msg"], report["type"])
except DynamicConfigError as error:
    print(error.kind, error.path, error.origin)
```

`DynamicConfigError` catches everything; each instance carries `kind`,
`path`, `origin_kind` and `origin` so a program can branch without
parsing English.

