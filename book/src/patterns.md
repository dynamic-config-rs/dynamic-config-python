# Patterns & Style

What using this well looks like from Python, and the mistakes that read
fine. The Rust book's [Patterns & Style](https://ctolon.github.io/dynamic-config/patterns.html)
covers the ones that are about the engine; these are the ones that are
about Python.

## One configuration per subsystem

```python
db = DynamicConfig(Database, key="db").file("config.toml").env("APP_")
cache = DynamicConfig(Cache, key="cache").file("config.toml").env("APP_")
flags = DynamicConfig(Values, key="flags").file("config.toml")
```

Three objects over one file, and they share nothing: three schemas, three
reloads, three failures that do not touch each other. A broken flags
section leaves the database's document serving.

**The flag table has no schema on purpose.** Its keys are a product
decision, and a class that had to declare each one would be edited every
time somebody added a flag — which is what `Values` is for.

## Read `current()` where you use it

```python
@app.get("/")
def index():
    db = config.current()          # here, not at import time
    return {"host": db.host}
```

`current()` is an attribute read on a cached instance — validation ran
once, at the reload. A module-level `DB = config.current()` is a value
that has stopped reloading, and it is the one mistake this library cannot
stop you making.

**In FastAPI**, the dependency is the configuration *object*:

```python
def database() -> Database:
    return config.current()

@app.get("/")
def index(db: Annotated[Database, Depends(database)]):
    ...
```

## Let the schema be the schema you already have

Pydantic if the program uses Pydantic, a dataclass if it does not want a
dependency, msgspec if the shape is hot, `Values` if there is nothing to
declare. The engine does not care, and swapping one for another changes
no other line. [What a schema may be](types.md#what-a-schema-may-be).

**Declare secrets where the field is**, not in a list somewhere else:
`SecretStr`, `field(metadata={"secret": True})`, or
`msgspec.Meta(extra={"secret": True})`. One declaration drives the
redaction in the cache, in `explain`, and in a scrubbed validation error.

## Hooks are for waking something, not for doing the work

```python
config.on_reload(lambda previous, current: pool.resize(current.pool.max_size))
```

A hook runs **inside** the reload, on the thread that noticed the change —
often the watcher's, and in an asyncio program that is *not* the event
loop. Anything that awaits belongs in `changes()` instead:

```python
async def follow():
    async for db in config.changes():
        await pool.resize(db.pool.max_size)
```

That is the asyncio shape: the iteration happens on the loop, and the
reload was over before it started.

## Testing without a filesystem

```python
with config.overrides(rate_limit=1, mode="test"):
    assert something_that_reads() == "test at 1/s"
```

Three doors, and none of them writes a file: `overrides` for a block,
`load()` for a candidate nobody installs, and defaults for the twelve
fields a test does not care about. The shipped pytest plugin gives you a
scratch directory and a clean environment as fixtures.

## What to check in CI, and what at startup

| Question | Where |
|---|---|
| Does the committed file still parse and validate? | CI: `DynamicConfig(...).check()` |
| Does *this deployment's* configuration load? | startup — `init()`, and let it raise |
| Is the store reachable? | a health endpoint, not a startup gate |

```python
@app.get("/healthz")
def healthz():
    status = config.status()
    code = 200 if status.consecutive_failures == 0 else 503
    return JSONResponse(status.__dict__, status_code=code)
```

## Type checking

The stubs are shipped, so `mypy --strict` sees your model through
`current()`. Two habits keep that true:

- **Annotate the configuration object**: `config: DynamicConfig[Database]`
  when it is a module-level name, so the generic parameter does not get
  lost.
- **`try_current()` when it may not be installed**: it is
  `Database | None`, which is what the checker wants at a boundary where
  `current()` would raise.
