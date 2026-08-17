# Async & asyncio

Nothing blocking ever runs on your event loop, and nothing on the loop is
required to use this library. Both halves matter: a service that reads
configuration should not stall its loop on disk I/O, and a script that
has no loop should not have to start one.

## The shape

```python
await config.init_async()          # load, validate, install
candidate = await config.load_async()
await config.reload_async()

model = await config.changed_async(timeout=30)   # the next install, once

async for db in config.changes():                # every install, forever
    await pool.resize(db.pool_size)

async for event in config.events():              # what installed, what was refused
    log.info("configuration %s", event)

async with config.running_async() as db:         # load, watch, serve, stop
    await serve(db)
```

Every `_async` method is the synchronous one performed on a worker
thread, with the GIL released for the blocking part — reading and parsing
files. What comes back onto the loop is the finished model. The
synchronous methods are not deprecated shadows of these; they are the
right call from a thread, a script or a test.

## Why a worker thread rather than "native async"

The engine's work is filesystem I/O and CPU: reading files, merging
layers, deserializing. There is no socket to await and nothing to
overlap, so an `async` implementation would still block a thread — it
would just be less honest about which one. Handing the work to an
executor and awaiting the result is what "async file I/O" means in
CPython anyway; `asyncio.to_thread` is the same mechanism.

This is the same decision the Rust crate makes. There, `load_async`
sends the load to a blocking worker — a fresh thread by default, or
tokio's blocking pool with the `tokio` feature — precisely so that no
executor thread is parked on a `read()`. The Python binding inherits the
policy rather than reinventing it.

## Which pool pays for the blocking half

By default the work goes to the event loop's own executor — the one
`run_in_executor(None, ...)` uses, shared with everything else in the
process that calls it. A service that would rather not queue behind an
unrelated batch job gives configuration its own:

```python
from concurrent.futures import ThreadPoolExecutor
import dynamic_config

dynamic_config.set_executor(ThreadPoolExecutor(2, thread_name_prefix="config"))

# the two-line version, which names the threads and closes the pool at exit
dynamic_config.configure_executor(2)

# for one block only, restored on the way out
with dynamic_config.executor(workers=4):
    await config.init_async()

# or for one configuration only
config = DynamicConfig(Database, key="db", executor=pool)
```

A pool passed to `set_executor` belongs to the caller and is never shut
down here. `configure_executor` builds one and owns it: it names the
threads — a dump that says `dynamic-config-blocking-0` answers a question
`ThreadPoolExecutor-3_0` does not — and closes it at interpreter exit.

This is the Python-side twin of the Rust crate's
`set_blocking_executor`, and answers the same question. What it is *not*
is tokio: the Rust `tokio` feature exists so that a Rust program's async
loads land in tokio's blocking pool rather than on a fresh thread. Here
the awaiting side is Python's loop, which cannot await a tokio task, so
the wheel does not carry tokio — it would be a runtime nobody awaits, in
every wheel, for every user. The executor above is the knob that
actually changes where the work runs.

**Waiting uses no executor at all.** Sizing this pool is about loads and
refreshes; a hundred tasks awaiting a reload occupy none of it. How that
works is the next section.

## Waiting for a reload

Two shapes, because two things want to wait:

**`changed_async(timeout=…)`** — one await, one answer. For a task that
needs the *next* configuration and then moves on. Returns `None` when the
timeout elapses first.

**`changes()`** — an async iterator over every install from here on. For
the long-lived task that follows configuration for the life of the
service:

```python
async def follow(config, pool):
    async for db in config.changes():
        if db.pool_size != pool.size:
            await pool.resize(db.pool_size)
```

**`events()`** — the diagnostic stream, described below.

### How a wait is answered

Until 0.2 an awaiting task polled: submit a quarter-second wait to an
executor, come back, submit again. It worked, and it cost one repeating
executor submission *per waiter* — a hundred of them for fifty
configurations with two consumers each — for the sole purpose of
noticing cancellation within 250 ms.

What answers a wait now is a notifier thread, one per configuration that
has async consumers:

```text
Rust watcher → install → generation++ → notify
                                          │
                 one notifier thread per configuration, shared
                                          │
                        loop.call_soon_threadsafe(future.set_result)
                                          │
                                    awaiting tasks
```

The thread parks in the engine with the GIL released and wakes for two
things only: an install, or the configuration being released. Every
awaiting task on that configuration shares it, so fifty configurations
with two consumers each park fifty threads rather than a hundred, and
none of them wakes until something installs.

Three consequences worth knowing:

- **Cancellation is immediate** — microseconds, not a quarter second.
  A cancelled task drops its future and stops awaiting; the engine is
  untouched, and a reload afterwards behaves exactly as it would have.
- **Nothing is polled**, so an idle service does no work at all for the
  configuration it is watching.
- The notifier thread ends at the first install that finds nobody
  waiting. Cancel every waiter and one thread stays parked on a
  condition variable until then — no timer, no CPU, and no way to
  reclaim it earlier without reintroducing the polling this replaced.

None of it is tied to asyncio's implementation details, so uvloop drives
it, and so does anything else that provides a running loop.

## The event stream

`changes()` is the model stream a service loop wants. `events()` is the
diagnostic one — what a log line, a metric or an alert is built from:

```python
async for event in config.events(failure_poll=1.0):
    match event:
        case Reloaded(generation=generation, changed=paths):
            log.info("config %s: %s", generation, ", ".join(paths))
        case ReloadFailed(kind=kind, path=path, consecutive=count):
            if count > 3:
                alert(f"configuration refused at {path}: {kind}")
```

**No event carries a value.** Paths, kinds, counts and timestamps only —
the same rule `explain()` and `check()` follow, and for the same reason:
a value in an event is a secret in a log.

`failure_poll` is what makes `ReloadFailed` possible. An install wakes
the stream; a refusal cannot, because the engine bumps no generation for
a load that installed nothing and there is nothing to be notified of. So
a stream that wants refusals asks for them, and pays one status read at
the interval it names. The default — `None` — starts no timer at all and
reports installs only.

## Loading several configurations at once

`DynamicConfig` is a value, so a service with a database file, a cache
file and a feature-flag file has three of them — and three loads that do
not need to queue:

```python
await asyncio.gather(
    database.init_async(), cache.init_async(), features.init_async()
)
```

Each keeps its own watcher, its own generation and its own followers, so
a flag flipping does not re-parse the database file or wake anything
watching it.
[`examples/13_asyncio_many_files.py`](https://github.com/dynamic-config-rs/dynamic-config-python/blob/main/dynamic-config-python/examples/13_asyncio_many_files.py)
is the whole shape, executor included.

### The same thing, on the model classes

The other shape is the decorator: the configuration lives *on the model
class*, so any module that can import `Database` can ask
`Database.current()` without being handed a configuration object first.
The async surface is reached through `Model.config`, and everything
above applies unchanged:

```python
@dynamic_config(key="db", files=["database.toml"], env="APP_")
class Database(BaseModel):
    host: str = "localhost"
    pool_size: int = 8

@dynamic_config(key="flags", files=["flags.toml"], env="APP_")
class Flags(BaseModel):
    new_checkout: bool = False

# Three files, one await; the loop is free while they are read.
await asyncio.gather(
    Database.config.init_async(), Cache.config.init_async(), Flags.config.init_async()
)

watch = await Flags.config.watch_async(debounce=0.25)

async for flags in Flags.config.changes():
    ...                                  # one follower per configuration
```

`Model.current()` stays synchronous everywhere — it is an attribute
lookup on a cached instance, so there is nothing to await, on the loop or
off it. The decorator does not load at import time (`init=False` is the
default), which is what makes decorating at module level safe: importing
a module should not begin filesystem work, and a loop that does not exist
yet cannot be the thing loading.

[`examples/14_async_decorator_services.py`](https://github.com/dynamic-config-rs/dynamic-config-python/blob/main/dynamic-config-python/examples/14_async_decorator_services.py)
runs three decorated services on one loop — concurrent loads, a watcher
and a follower each, and generations that prove one team's edit left the
other two configurations alone.

## Several configurations, one lifecycle

`asyncio.gather` above is the two-line version. When the same service
starts, watches and stops five configurations, the orchestration itself
is worth naming:

```python
group = ConfigGroup(database, cache, queue, concurrency=2)

async with group.running_async():      # init all, watch all, stop all
    await serve()
```

The group owns *lifecycle*, not storage: `database.current()` is still
the read path, and nothing sits between a program and its values.
`concurrency` bounds how many members load at once — `None`, the
default, loads them one at a time, which is what a handful of small files
wants.

For a health endpoint, one call:

```python
{key: status.is_healthy for key, status in group.status().items()}
```

### All of them, or none of them

The mixed state worth preventing: a deployment moves three files, two
parse and one does not, and the process runs on two new documents and one
old one — with nothing in any of them saying so.

```python
group.reload_atomic()          # every member validates, or none installs
await group.reload_atomic_async()
```

Every member loads and validates first; only when all of them have does
any of them install. A refusal names the member it came from and leaves
every snapshot exactly as it was, generation included. It is the engine's
own `ReloadGroup` — prepare-then-commit, which Rust callers have had
since 0.4 — driven from Python.

`group.reload()` is the other half of the contract and does *not* do
this: each member reloads independently, and one refusing leaves the
others on their new documents. That is the right behaviour for
configurations that are unrelated, and the wrong one for configurations a
deployment moves together.

## Callbacks that are not free

A hook registered with `on_reload` runs on the thread that installed —
the watcher's, or the caller's — and the reload waits for it. That is the
right default for a hook that compares two numbers and signals a
subsystem, and the wrong one for a hook that rebuilds a connection pool.

```python
@config.on_reload_async                       # a task on this loop
async def reconnect(previous, current):
    await pool.resize(current.pool_size)
```

The watcher schedules the task with `call_soon_threadsafe` and moves on,
so reload latency and callback latency stop being one number. Two
parameters spell out the rest:

| `dispatch` | Where the hook runs |
|---|---|
| `Dispatch.INLINE` | On the installing thread, before the reload returns. The default. |
| `Dispatch.EXECUTOR` | On the configuration executor — for work that is slow but synchronous. |
| `Dispatch.ASYNCIO` | As a task on the loop that registered it. The default for a coroutine function, and the only value that accepts one. |

| `backpressure` | When installs outrun the hook |
|---|---|
| `Backpressure.EVERY` | One call per install. The only policy an inline hook can have. |
| `Backpressure.LATEST` | Coalesce: keep the newest install and run it next. The default off the installing thread. |
| `Backpressure.SERIAL` | Queue every install and run them in order, dropping nothing. |
| `Backpressure.CANCEL_PREVIOUS` | A new install cancels the call still running. `asyncio` only. |

Both are `str` enums, so `dispatch="executor"` works and a value read out
of a configuration file is accepted as it is — and a typo is a
`ValueError` at registration rather than a callback that silently never
runs.

`latest` is the default off the installing thread because it is what
configuration usually means: resizing a pool to a size nobody is asking
for any more is work done for nothing. `serial` is for a hook that is a
log rather than a reconciliation, where a gap is a broken audit trail.

An async hook must be registered **from the loop that should run it**.
There is no loop on a watcher thread to fall back on, so registering
without one raises rather than scheduling onto nothing.

## A remote store with an async client

`RemoteSource` cannot hold an `httpx.AsyncClient`: the engine calls
`fetch()` from a worker thread, and a coroutine returned there is an
object nobody awaits. `AsyncRemoteSource` is the other door:

```python
class ControlPlane(AsyncRemoteSource):
    async def fetch(self):
        async with httpx.AsyncClient() as client:
            response = await client.get(URL, timeout=5)
            response.raise_for_status()

            return response.text, Format.JSON

    def describe(self):
        return "the control plane"

config = DynamicConfig(Database, key="db").remote(ControlPlane())

await config.refresh_remote_async()
await config.reload_async()
```

The coroutine is awaited on the loop that called `refresh_remote_async`,
and only the merge that follows goes to a thread. Two things follow from
that ordering: a raising `fetch()` reaches the caller as its own
exception rather than as `RemoteError`, because nothing has entered the
engine yet — and cancelling the refresh cancels the fetch, which a worker
thread could never have offered.

The synchronous `refresh_remote()` raises on such a store. Hiding a
private event loop behind it would work until the day it did not: an
async client built on one loop and driven from another is a class of bug
that surfaces days later, as a hang.

## Reading inside a request

```python
@app.get("/health")
async def health():
    db = config.current()      # once, at the top
    await do_work(db.host)
    return {"host": db.host}   # the same value, whatever landed meanwhile
```

The same line works in a synchronous endpoint — FastAPI runs those on a
worker thread — because the read is thread-safe and needs no loop: the
model is immutable and the swap is atomic, so a reload landing
mid-handler cannot tear the value in hand. See
[Web Frameworks](frameworks.md) for both styles side by side.

`current()` is an attribute lookup — no `await`, no boundary crossing, no
lock a writer can hold. Read it once per request and use that value for
the whole request: a reload landing halfway through would otherwise show
one request two configurations, which is the one bug hot reload
introduces if you let it.

## Watching, from a loop

`config.watch(...)` starts a background thread, not a task, because the
filesystem notification backend is a thread-shaped thing on every
platform. It needs no loop and does not interact with yours: when a
reload lands, it validates on the watcher thread, publishes, and wakes
whatever is awaiting `changes()` on your loop.

```python
watch = await config.watch_async(debounce=0.25)
watch.detach()        # for the life of the process
```

For a service whose lifetime is a block, the shape that cannot leak the
handle by forgetting to stop it:

```python
async with config.watching_async(debounce=0.25):
    await serve()

async with config.running_async() as db:      # init, then watch, then stop
    await serve(db)
```

`watch_async`, and not because the watcher needs it — it is a thread
either way. What the await moves off the loop is *starting* it:
resolving the directories to observe, registering each with the
notification backend, spawning the carrier thread. Natively that is a
fraction of a millisecond, growing with the number of directories.
`poll_interval` is the case that earns the twin: the poll backend takes
a baseline scan of everything it watches before it can report a change,
which measures single-digit milliseconds over a large directory and
worse over the network filesystems that are the reason to poll at all. A
startup handler runs once and would survive the sync call; a loop that
is answering requests should not be the thing waiting on `readdir`.

Stopping needs no twin. `Watch.stop()` drops the backend, which closes
the channel the watcher thread is parked on, and returns — it does not
join the thread and does not wait out a debounce window, so a reload
already in flight finishes on its own thread while `stop()` has long
returned. Call it directly from a shutdown handler.

Hooks registered with `on_reload` also run on the watcher thread. A hook
that needs to touch loop-owned state should say where it wants to run
rather than hopping by hand:

```python
@config.on_reload_async                  # a task on the registering loop
async def refresh(previous, current):
    await queue.put(current)
```

The hand-written version is still exactly what happens underneath, and
is worth knowing for the cases the parameter does not cover:

```python
loop = asyncio.get_running_loop()
config.on_reload(lambda old, new: loop.call_soon_threadsafe(queue.put_nowait, new))
```

That is the same advice the Rust
[reload lifecycle](https://dynamic-config-rs.github.io/reload-lifecycle.html) gives: compare, then signal
the thing that owns the resource.
