# Pre-forking Servers

gunicorn, uWSGI, mod_wsgi and Django's `runserver` all reach the same
place: several processes serving from one application, forked from one
parent. Configuration survives that fork. A watcher does not.

## What each process gets

**Four workers are four engines are four watches.** There is no shared
snapshot, no master coordinating reloads, no cross-process anything:
each worker owns a full copy of the engine, reloads on its own schedule
from the same file, and none of them has to tell the others. Every
multi-process surprise below is that sentence, met somewhere
unexpected — a metric that counts one worker's reloads, a `set_override`
that patched one process, an `on_reload` hook that fired a quarter of
the times you expected.

Each worker gets its own copy of everything, including its own watcher, and
that is what you want.

```console
$ gunicorn -w 4 --preload myapp:app
```

With `--preload` the module-level `init()` runs **once, in the master**, and
the loaded snapshot is inherited by all four children. One parse, four
workers.

## The rule

**Start the watcher after the fork.** A watcher is a thread, and a thread
does not survive `fork()`: the child inherits the engine's registration but
not the thread that feeds it, so a watcher started in the master leaves four
children that will never reload and one master that is not serving.

```python
# gunicorn.conf.py
def post_fork(server, worker):
    from myapp.config import database

    database.watch(debounce=0.25)
```

uWSGI's equivalent is `@postfork`, and `lazy-apps = true` sidesteps the
question entirely by building the application in each worker:

```ini
[uwsgi]
master = true
processes = 4
lazy-apps = true
```

## What `dynamic-config-py-web` does about it

Nothing you have to write. The adapters lease the watcher per configuration
and re-arm it in a forked child, so `--preload` works with no hook at all:

```sh
pip install "dynamic-config-py[fastapi]"
```

See that package's [Deployment](https://dynamic-config-rs.github.io/web/deployment.html)
chapter for the per-server table.

## Checking it

Four workers should be four watchers, not one:

```python
import threading

print([t.name for t in threading.enumerate() if "dynamic-config" in t.name])
```

If a worker shows none and reloads never arrive, the watcher started before
the fork.

## The whole thing, runnable

Three files; `gunicorn -c gunicorn.conf.py -w 4 --preload app:app` and
then edit `config.json` while it serves.

```python
# config.py — the declaration, imported by both app and conf
from dataclasses import dataclass
from dynamic_config import DynamicConfig


@dataclass
class Database:
    host: str = "localhost"
    pool_size: int = 8


database = DynamicConfig(Database, key="db").file("config.json")
```

```python
# app.py — a WSGI app; init at import time so --preload parses once
import json

from config import database

database.init()


def app(environ, start_response):
    current = database.current()  # this worker's snapshot, this instant
    start_response("200 OK", [("Content-Type", "application/json")])

    import os
    return [json.dumps({"pid": os.getpid(), "pool": current.pool_size}).encode()]
```

```python
# gunicorn.conf.py — the one hook that matters
def post_fork(server, worker):
    from config import database

    database.watch(debounce=0.25)  # this worker's watcher, born after the fork
```

Edit `config.json` and every worker's next response carries the new
value — four pids, one document, no coordination. Delete the
`post_fork` hook and rerun to watch the failure mode itself: the same
edit changes nothing, in any worker, forever.
