# Pre-forking Servers

gunicorn, uWSGI, mod_wsgi and Django's `runserver` all reach the same
place: several processes serving from one application, forked from one
parent. Configuration survives that fork. A watcher does not.

## What each process gets

Each worker gets its own copy of everything, including its own watcher, and
that is what you want — every worker reloads independently from the same
file, and none of them has to tell the others.

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
