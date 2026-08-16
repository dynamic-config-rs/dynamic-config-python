---
name: change-python-bindings
description: Use when changing anything in dynamic-config-python, or when a change to the Rust core's public surface has to reach Python — covers the two-halves layout, what must move together (facade, stub, reference, tests), the invariants a change must not break, and how to build and run the suite.
---

# Changing the Python bindings

The binding is two halves that have to agree, plus four documents that go
stale silently. Most of the work in a change here is *the list*, not the
code.

## The layout

```text
dynamic-config-python/
  src/                       the compiled half — `dynamic_config._core`
    lib.rs                   module registration, the two version strings
    config/                  the engine object: sources, lifecycle, hooks
      mod.rs                 the Config class itself, and Watch/Snapshot
      inner.rs               the shared state a hook and a load both touch
      scrub.rs               a Python failure, minus the values it names
      handles.rs             the objects handed back out
    convert.rs               resolved tree ⇄ Python data
    errors.rs                the exception hierarchy, mirroring ErrorKind
  python/dynamic_config/     the facade, one concern per file
    __init__.py              the public surface: re-exports, nothing else
    _config.py               DynamicConfig: sources, lifecycle, hooks
    _dataclasses.py          a plain dataclass as a schema
    _decorator.py            @dynamic_config
    _diagnostics.py          Origin, Explanation, Report, Snapshot, …
    _dispatch.py             Dispatch, Backpressure — where a hook runs
    _errors.py               NotInitialisedError
    _events.py               Reloaded, ReloadFailed — what events() yields
    _executor.py             set_executor, configure_executor, executor
    _group.py                ConfigGroup: several configurations, one lifecycle
    _lifetime.py             Watch, HookGuard, the atexit sweep
    _notify.py               the notifier thread that answers an await
    _msgspec.py              a msgspec Struct as a schema
    _pydantic.py             a Pydantic model as a schema
    _schema.py               which adapter a class gets; the shared questions
    _settings.py             the pydantic-settings bridge
    _values.py               Values: a configuration with no schema
    _core.pyi                stubs for the compiled half
    py.typed
  tests/                     pytest, one file per concern
    test_dataclasses.py      the dependency-free schema, and its refusals
    test_msgspec.py          the msgspec schema, and the three answers of its own
    test_pydantic.py         the class surface, aliases, BaseSettings
    test_integration.py      whole scenarios, and the shipped examples
  examples/                  twenty-seven runnable scripts, all run in CI
  benchmarks/read_path.py    the numbers the book quotes
```

Rust owns the engine, the conversion and the one place Python is entered
on a reload. Python owns typing, the decorator and the event loop. Put a
change on the side where it is *clearer*, not the side where it is
faster to write — the read path is the only hot one, and it is already
an attribute lookup.

## What moves together

Adding or changing a method on `_core.Config` means **all** of these:

1. `src/config.rs` — the `#[pymethods]` entry.
2. `python/dynamic_config/_config.py` — the facade wrapper, with a
   docstring (ruff's `pydocstyle` fails the gate without one), and
   `__init__.py` if the name is public.
3. `python/dynamic_config/_core.pyi` — the stub, or `mypy --strict`
   stops seeing through the boundary.
4. `book/src/reference.md` — the API reference table. A method
   with an async twin goes on the *same row* as its twin.
5. `tests/` — the behaviour, not the call. A call a *service* makes
   belongs in `test_integration.py` as well, exercised the way a service
   would reach it (through a framework, under a watcher, with threads
   reading).
6. `dynamic-config-python/CHANGELOG.md` — under `Unreleased`.

Adding a **source** option means the same list plus the decorator's
argument table (`dynamic_config(...)` in the facade, and the table in
`reference.md`).

## The invariants a change must not break

Each of these has a test; if a change needs one of them relaxed, that is
a design conversation rather than a patch.

- **Validation runs once per successful resolve.** Not per read, not
  twice per reload. `tests/test_performance.py` counts it with a
  validator that increments a counter.
- **A read never crosses into Rust.** `current()` is `self._cached`, kept
  fresh by a hook registered at construction. If you find yourself
  calling `_core.current()` on a read path, stop.
- **The two caches agree.** The Python-side cache and the engine's own
  are asserted equal after every install path — init, reload, watch,
  replace, recovery.
- **A rejected reload changes nothing.** Validation is the engine's
  `validate` hook, which runs *before* the install: no install, no cache
  write, previous model still serving.
- **Values stay out of diagnostics.** Every `repr` is shape-only;
  `explain` is the exception and redacts secrets; `ValidationError` is
  rebuilt scrubbed rather than re-raised.
- **Pydantic is optional, and nothing at import time may assume it.** The
  base install has no dependencies; `_schema.py` picks an adapter by
  walking the MRO for a *name*, and `_pydantic.py` is imported only once
  a Pydantic class has been passed. A module-level `from pydantic import
  ...` anywhere else breaks `pip install dynamic-config-py`, and CI
  proves it in a bare virtualenv rather than trusting the review.
- **Nobody re-declares secrets.** They come from `model_fields`, under
  **every** name a file could use — the field name plus each alias shape
  (`AliasChoices`, `AliasPath`, `alias`, an `alias_generator`), and
  through Pydantic dataclasses and `RootModel`. Picking one name per
  field was a real leak: `explain` and the "redacted" cache both carried
  the value when a file used any other spelling.
- **A settings class declares sources this engine ignores.** A
  `BaseSettings` whose `SettingsConfigDict` names `env_prefix`,
  `env_file` or a config file gets none of it under `model_validate`, so
  `DynamicConfig` warns and `from_settings` translates. A change that
  adds a source option should ask whether pydantic-settings has a
  spelling for it.
- **Nothing touches Python from `Drop`,** and `atexit` stops every
  watcher before finalization.

## The GIL rules that have already bitten

- **Never hold a Rust lock while waiting for the GIL.** A second thread
  blocked on that lock is a thread holding the GIL the first one needs.
  The engine is cloned out of its mutex (`Arc`) before anything slow.
  This was a real deadlock, found by `tests/test_threading.py`.
- **Release the GIL for anything that reads files** (`py.detach`), and
  let the validate hook re-acquire it.
- **A wait uses no executor and no timeout.** One notifier thread per
  configuration (`python/dynamic_config/_notify.py`) parks in
  `wait_for_change(seen, None)` and resolves futures with
  `loop.call_soon_threadsafe`. Two rules follow: the Rust wait must end
  on `release()` as well as on an install (`Wake.closed`), and every
  resolution carries the generation it is for — a consumer that noticed
  an install by reading the generation itself would otherwise report the
  notifier's answer for the same install a second time.
- **Releasing the GIL is not the same as not blocking the loop.** An
  event loop runs on the calling thread, so a `py.detach`-wrapped
  syscall still stalls it. That is why `watch` has an `_async` twin even
  though the watcher is a thread: *starting* it registers directories
  (milliseconds when polling scans a large one). Anything that reaches
  the filesystem needs a twin, however briefly.

## Building and running

```sh
just python        # maturin develop + pytest + mypy + ruff + every example
just python-bench  # the read-path numbers
```

Ruff is both linter and formatter here (`ruff check .`, `ruff format
--check .`), configured in `pyproject.toml`. Two things about that
configuration are load-bearing rather than taste: `pydocstyle` is on, so
an undocumented public definition fails the gate, and PEP 604 (`X |
None`) is **disabled** — Pydantic evaluates a model's annotations when
the class is built, so `from __future__ import annotations` does not make
that syntax safe at the 3.9 floor, and neither mypy nor vermin would say
so.

By hand, in a virtualenv (`pip install -e 'dynamic-config-python[dev]'`,
plus `fastapi flask django httpx` for the framework examples):

```sh
cd dynamic-config-python
maturin develop
python -m pytest tests -q
mypy --strict python/dynamic_config/
ruff check . && ruff format --check .
python examples/01_quick_start.py
```

There is no `cargo test` for these crates: an extension module links no
libpython, so neither has a test target. The suite is pytest, and CI runs
it across every supported interpreter.

## Two things about versions

- **Both wheels version together**, and neither follows the engine: the
  engine is a caret dependency from crates.io, so its releases arrive
  without one here. `./scripts/release-python.sh patch` moves both
  manifests, both changelogs and the floor between them in one commit.
- **The floor is CPython 3.9.** `vermin --target=3.9- python/ tests/
  examples/` says whether that is still true, and CI runs the suite on
  3.9 through 3.14. `X | Y` in a runtime position is the usual way to
  break it; annotations are fine because the modules use
  `from __future__ import annotations`.

## Things that have bitten

- **Two commit paths, one install.** The engine's reload hook and the
  explicit publish after `init`/`reload` both fire; without the sequence
  number stamped at validation, every reload ran every hook twice.
- **A strong reference from a hook to the configuration** is a cycle
  through a `#[pyclass]`, which Python's collector cannot traverse —
  nothing would ever be freed. Hooks hold weak references.
- **`model_dump()` keeps `SecretStr` objects**, so a conversion that does
  not understand them refuses a model with a secret in it. Comparing the
  *mask* instead would make two different passwords look equal.
- **A second `watch()` on one configuration is `AlreadyExists`** — which
  a framework startup handler will hit the second time it runs
  (`uvicorn --reload`, a test building a second client). Make such
  handlers idempotent.
- **PyO3 renames things between versions**: `downcast` → `cast`,
  `with_gil` → `attach`, `allow_threads` → `detach`. Compile early rather
  than writing a large change against a remembered API.
