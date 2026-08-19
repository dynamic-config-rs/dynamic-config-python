# Changelog

All notable changes to `dynamic-config-py` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Before 1.0, a breaking change bumps the **minor** version and anything else
bumps the patch. A change to the minimum supported Python version is
breaking.

<!-- Keep this template. Add entries under `Unreleased` as you go, and move
     the whole block under a new version heading at release time.
     (Spelled `_Unreleased_` here so cargo-release's `exactly = 1` search
     for the real heading matches only the real heading.)

## [_Unreleased_]

### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security

-->

## [Unreleased]

### Fixed

- **A wake could be lost when an install raced the notifier thread's
  first breath.** The thread read its baseline generation on its own
  first instruction — so an install landing after a waiter's
  check-register-check but before the thread was scheduled folded into
  the baseline and was never reported; under load, `changed_async`
  timed out on an install that had already happened. The baseline is
  now read on the caller's thread, under the registration lock, before
  `wait` returns — the same discipline for `events()`' notifier, which
  carried the identical pattern from birth.

### Changed

- **Built on engine 0.8.** The engine's breaking release (a `LoadSpec`
  field, MSRV 1.88) is a build-time fact here — the wheel embeds it
  statically and the Python surface is unchanged, which is why this
  release stays a patch. The development-only `[patch.crates-io]`
  block is gone: wheels build from exactly what crates.io serves.

### Changed

- **A refused reload wakes `events()` natively.** The engine's 0.7.1
  failure hook signals the same parked notifier thread an install does,
  so `ReloadFailed` arrives when the refusal happens rather than at the
  next poll — and the stream starts no timer at all. Delivery is
  latest-wins, like `changes()`: coalesced refusals arrive as one event
  carrying the current `consecutive` count, and a refusal followed by an
  install arrives as both events, refusal first. The limitations page's
  "a refused reload cannot wake anything" section retires with this.

### Deprecated

- **`events(failure_poll=...)`** is accepted, ignored, and warns once:
  the interval refusals were polled at, now that they wake the stream
  themselves. Remove the argument; the parameter goes away in 0.4.

## 0.3.0 — 2026-08-18

### Fixed

- **A free-threaded interpreter could segfault on exit mid-reload.** A
  watcher thread attaching to Python while finalization tears the
  runtime down crashed free-threaded 3.14 (the GIL build used to park
  such a thread instead). The `atexit` sweep now closes a finalization
  gate first: background attaches from that point on are refused —
  reloads skipped, not delivered to a dying interpreter — and the ones
  in flight are waited out while the interpreter is still whole. This
  also makes `Watch.detach()`'s "built to survive exit" promise true on
  free-threaded builds, which is where it was not.

### Changed (book)

- **The book opens with a Quick Start**, and the 477-line introduction
  became three pages: the pitch, Core Concepts, and The Decorator &
  Typing. The 991-line Rust-stores page split the same way: narrative,
  a credentials/TLS/runtime cookbook, and a per-store reference.

### Changed

- **The engine's diagnostics arrive through `logging` now.** ⚠️ From the
  first import, the lines the compiled engine used to write straight to
  file descriptor 2 — `[dynamic-config] db#1: reloaded in 0ms`, the
  failed-reload warnings, the last-known-good recovery notice — are
  ordinary records on `logging.getLogger("dynamic_config.engine")`.
  Handlers, formatters, filters, `caplog` and structured logging all see
  them; nothing is written to raw stderr any more unless asked.

  The bridge never takes the GIL on an engine thread: lines cross a
  bounded channel to one forwarder thread, overflow is counted and
  reported in the next delivered record, and the whole thing stands down
  at interpreter exit. A script that configures no logging still sees
  warnings via `logging`'s last-resort handler; the INFO reload lines
  become opt-in, which is the one visible change.

  `configure_logging(level=...)` sets the engine-side volume in
  `logging`'s units, and `configure_logging(raw_stderr=True)` restores
  the 0.2 behaviour wholesale for the deployment that greps stderr.

## 0.2.0 — 2026-08-18

### Added

- **Nine extras that resolve to the web adapters.**
  `dynamic-config-py[fastapi]`, `[litestar]`, `[flask]`, `[quart]`,
  `[django]`, `[drf]`, `[ninja]`, `[robyn]` and `[django-bolt]` — plus
  `[web]` for the shared core with no framework — each resolving to
  [`dynamic-config-py-web`](https://github.com/dynamic-config-rs/dynamic-config-python-web),
  which is where the wiring, the request scope, the health surface and the
  test doors now live. Not in `[all]`, exactly as `[remote]` is not:
  `[all]` means the schema libraries.

  The book's *Web Frameworks* page keeps the two rules and the hand-written
  version — the adapters are what those rules look like when they are
  checked rather than recommended — and points at that package's book for
  the installed one.

- **`ConfigGroup`: several configurations under one lifecycle.**
  `ConfigGroup(db, cache, queue)` initialises, watches, reports and stops
  its members together, with async twins throughout and `concurrency=` to
  bound how many load at once. `group.status()` and `group.generations()`
  answer per key, for a health endpoint. The group owns lifecycle, not
  storage: `db.current()` is still the read path.

- **`group.reload_atomic()`: every member validates, or none installs.**
  The engine's prepare-then-commit — `ReloadGroup`, which Rust callers
  have had since 0.4 — driven from Python. A refusal leaves every
  snapshot exactly as it was, generation included, instead of leaving a
  deployment half-applied across two configurations.

- **`events()`: installs and refusals as typed events.** An async
  iterator of `Reloaded(generation, at, changed, reason)` and
  `ReloadFailed(generation, at, kind, path, consecutive)` — frozen
  dataclasses a `match` reads as prose. **No event carries a value**, the
  same rule `explain()` and `check()` follow. `failure_poll=` opts into
  checking for refusals, which nothing can wake a stream for: the engine
  bumps no generation for a load that installed nothing.

- **Reload hooks can say where they run.** `on_reload(hook,
  dispatch=..., backpressure=...)`, plus `on_reload_async` and
  `on_change_async` for coroutine functions. `Dispatch` is `inline`
  (the default, unchanged), `executor` or `asyncio`; `Backpressure` is
  `every`, `latest` (the default off the installing thread), `serial` or
  `cancel_previous`. Both are `str` enums, so `dispatch="executor"` works
  and a typo is a `ValueError` at registration rather than a callback
  that silently never runs. A coroutine function registered with no
  `dispatch` now runs as a task instead of being called inline and
  returning a coroutine nobody awaits.

- **`AsyncRemoteSource`: a remote store whose client is async.** Its
  `fetch()` is awaited on the loop that called `refresh_remote_async()`,
  so an `httpx.AsyncClient` runs on the loop it was built on; cancelling
  the refresh cancels the fetch, and a raising `fetch()` reaches the
  caller as its own exception rather than as `RemoteError`. The
  synchronous `refresh_remote()` raises on such a store rather than
  driving it from a private loop.

- **The lifetime as a block.** `with config.running():` is init, then
  watch, then stop; `config.watching()`, `group.watching()`,
  `group.running()` and the `_async` twin of each do the same for the
  pieces. The shape that cannot leak a watcher by forgetting to stop it.

- **`configure_executor(workers)` and `executor(...)`.**
  `configure_executor` builds the blocking pool, names its threads
  `dynamic-config-blocking-N` and shuts it down at exit; `executor()` is
  the same choice as a block, restored on the way out. `set_executor` is
  unchanged, and the pool passed to it is still never shut down here.

- **`Model.config` keeps the model's type.** A configuration reached
  through the decorator was `DynamicConfig[Any]`, so everything reached
  through *it* — `Model.config.current()`, `changes()`,
  `changed_async()` — came back as `Any` under `mypy --strict` while
  `Model.current()` was correctly typed. `Configured` now declares it as
  a descriptor generic over the class it is read from, which is how
  `classmethod` itself is typed, so `Database.config` is
  `DynamicConfig[Database]`. Runtime behaviour is unchanged, and the
  `ClassVar` Pydantic needs is still what Pydantic sees.

### Changed

- **The book has parts** — *Guide*, *Use Cases*, *Advanced* and *Reference*
  — and pre-forking servers move out of the bottom of *Web Frameworks* into
  a chapter of their own.

- **Awaiting a reload no longer polls.** `changed_async()`, `changes()`
  and `events()` are answered by one notifier thread per configuration —
  shared by every awaiting task on it, parked in the engine with the GIL
  released, and woken only by an install or by release. Before this, each
  waiter re-submitted a quarter-second wait to an executor for as long as
  it waited.

  What changes for callers: **cancellation is immediate** rather than
  within 250 ms; an idle service does no work at all for the
  configuration it is watching; and the executor is free for loads, so a
  hundred awaiting tasks no longer contend with the reload they are
  waiting for. `set_executor` still answers the same question it did.

- **Watcher-side threads are named.** A notifier thread is
  `dynamic-config-notify-<key>` and a dispatched hook's thread is
  `dynamic-config-hook`, so a thread dump says which configuration it is
  looking at.


## 0.1.3 — 2026-08-16

### Changed

- **The wheels moved to their own repository**,
  [dynamic-config-rs/dynamic-config-python](https://github.com/dynamic-config-rs/dynamic-config-python),
  and release on their own schedule from there. The package name, the
  import name and the API are unchanged; what moved is where issues are
  filed and where the book lives —
  [dynamic-config-rs.github.io/python/](https://dynamic-config-rs.github.io/python/).

- **The project links on PyPI say more.** `Homepage` is the book rather
  than a Rust workspace's front page, `Documentation` points at the API
  reference, and `Issues` was added — a reader arriving from PyPI now
  lands on Python prose in one click.

- **More keywords and classifiers**, because both are how PyPI's search
  and filters find a package at all: `Framework :: Pydantic`,
  `Framework :: AsyncIO`, the systems-administration and distributed
  topics, and the words a person actually types (`dotenv`, `toml`,
  `twelve-factor`, `live-reload`).

- **The engine and the store crates are named with a caret** in the
  manifests these wheels are built from, so a patch release of either
  reaches a source build without a release here.

## 0.1.2 — 2026-08-14

### Added

- **`Values.sub(path)`**: the subtree at a path, as a `Values` of its own.
  What a subsystem gets handed instead of the whole configuration —
  relative paths below it, so a function that takes a `Values` does not
  have to know where in the document it lives. `Snapshot::sub` is the Rust
  equivalent, and this was the one shape that had no way to say it: a
  caller indexed twice at every read, or built a dict and lost the
  dotted-path lookup that is `Values`' whole point.

  A path that holds nothing — or holds a value rather than a table —
  answers an *empty* `Values` rather than raising. A subsystem handed a
  section its deployment did not configure should reach its own defaults
  rather than crash on the way to them, and `in` tells the two apart.

### Fixed

- **An unknown key reads as a sentence in `check()`'s report.** It rendered
  the dataclass `repr` — `UnknownKey(path='stray', suggestion=None)` — in the
  middle of a table of prose, and it is the one line of a report a person has
  to act on. It now says `stray: unknown key`, and `did you mean …?` when
  there is a suggestion, which is what the Rust crate has always printed.

### Added

- **`msgspec.Struct` is a schema.** The fifth kind of declaration, and
  the fastest at what a reload asks of one: `pip install
  dynamic-config-py[msgspec]`, then pass the struct where a model would
  go. Everything around it is unchanged — the same sources, watcher,
  cache and diagnostics — and the three answers that are msgspec's own
  are documented rather than left to be discovered: a secret is declared
  with `Meta(extra={"secret": True})`, unknown keys are the struct's
  business (`forbid_unknown_fields`), and `InvalidError.errors` is empty
  because msgspec raises a message rather than a report. A secret under a
  *container* — `list[Credentials]` — redacts the containing field whole,
  because a dotted path cannot index a list, and the direction to be wrong
  in is the one that keeps passwords out of a cache. Decoding is lax,
  because an environment variable is a string. `examples/22_msgspec.py`.
- `changed_paths` and `set_default` accept a `msgspec.Struct` instance,
  under the names a file writes rather than the Python ones — a struct
  declaring `rename="camel"` diffs as `maxSize`, which is the key every
  other path in this library already uses.

### Changed

- **`InvalidError.errors` is present on every refusal**, and `[]` when
  the schema raised a message rather than a report. It used to be absent
  for a dataclass schema, so `error.errors` was an `AttributeError` that
  depended on which schema library the configuration happened to use —
  while the shipped stub declared it unconditionally.

### Fixed

- A msgspec `ValidationError` no longer carries the value it refused into
  a diagnostic. Two of msgspec's messages quote the data — an enum member
  and a tagged union's tag — and this boundary takes it back out, keeping
  the path, which is field names rather than data.

### Changed

- One classifier per interpreter the CI matrix actually runs (3.9 through
  3.14), plus CPython and the Python-modules topic: PyPI's filter reads
  those rather than `requires-python`.

## 0.1.1 — 2026-08-14

### Added

- **`Values`: a configuration with no schema class.** The Python half of
  the crate's `Dynamic<Value>`, for the keys a program learns at run time
  rather than declares — a plugin host, a feature-flag table, a tool
  reading a file it did not write. Pass the class where a model goes and
  every load hands back a `Mapping` read by dotted path
  (`values["cache.ttl"]`), with every value already a plain Python
  object. The same layers, profiles, watcher, cache, hooks and
  diagnostics as any other configuration.

  It gives up exactly what it never declared, and says so rather than
  assuming: `check()` reports `unknown_checked = False` — an empty
  unknown-key list from a configuration with no field names is not an
  all-clear — and a `redacted` or `fingerprint` cache is **refused**
  until `DynamicConfig(Values, key=…, secrets=[…])` names what to
  redact, rather than writing a file that claims a redaction it did not
  perform. `secrets=` works for a declared model too, where it adds to
  what the model already says. `examples/20_schemaless.py` runs all of
  it.

- **`Report.unknown_checked`**, and a `str(report)` that renders the same
  table the Rust crate prints. Without the flag, a report from a
  configuration with no field list was indistinguishable from a clean
  one.

- **`dynamic_config.remote.TlsConfig`.** The remote wheel's TLS vocabulary,
  re-exported here with the stores — a private certificate authority and a
  client certificate, as file paths or PEM bytes. Nothing in this wheel
  changes: the re-export list is asserted equal to the remote package's own
  `__all__` by that package's suite, so a name added there and not here
  fails a test rather than reaching a user as an `ImportError` from the
  dotted path the documentation tells them to write.

- **The Rust remote stores, as an opt-in second wheel.** `pip install
  dynamic-config-py[remote]` buys the compiled **Consul, etcd, Firestore,
  NATS, Redis, S3 and Vault** clients, imported as
  `dynamic_config.remote`. A pip extra installs
  distributions and cannot turn on a Cargo feature in a binary compiled
  weeks ago, so it resolves to a distribution of its own —
  `dynamic-config-py-remote`, built from the new `dynamic-config-python-remote`
  crate. The base install is unchanged and importing `dynamic_config.remote`
  without the extra raises an `ImportError` naming it. Both stores are
  ordinary `RemoteSource` implementations, so the base wheel needed no
  change to accept them: nothing Rust crosses between the two extension
  modules, only a `(document, format)` pair. **Every credential argument
  accepts a callable**, resolved on every fetch, because a watcher outlives
  its credentials — a value that has changed rebuilds the client, and one
  that has not leaves the store's own token cache alone. One tokio runtime,
  lazily, owned by the remote module and started when the first store that
  needs one is constructed; the base wheel still refuses the engine's
  `tokio` feature. See the book's
  [Remote Stores in Rust](https://dynamic-config-rs.github.io/python/remote-wheel.html).

  `dynamic_config/remote.py` re-exports exactly what `dynamic_config_remote`
  exports, and that is now asserted as a **set equality in both suites**.
  It previously checked only that every name here existed over there, which
  a base wheel re-exporting *fewer* names satisfies — so the five stores
  after etcd and Vault were reachable as `from dynamic_config_remote import
  Redis` while `from dynamic_config.remote import Redis`, the dotted name
  the documentation tells people to write, was an `ImportError`. The
  direction that catches a base wheel falling behind is the one that was
  missing.

- **A remote store can be written in Python.** `RemoteSource` is an ABC
  with two abstract methods, and an instance of a subclass is a store the
  Rust engine fetches from:

      class ConfigService(RemoteSource):
          def fetch(self):
              return httpx.get(URL, timeout=5).text, Format.JSON

          def describe(self):
              return "the config service"

      config = DynamicConfig(Database, key="db").remote(ConfigService())
      config.refresh_remote()
      config.init()

  New on `DynamicConfig`: `remote(source)` (chains, and unlike the other
  source methods is allowed after the first load, exactly as the Rust
  `set_remote` is), `refresh_remote()`, `refresh_remote_async()`,
  `clear_remote()` and the `remote_description` property. New on the
  package: `RemoteSource` and `Format`. Fetching is explicit as it is in
  Rust — a load merges the document last fetched and touches no network —
  and the remote layer sits above the files and below the environment.

  Three decisions are worth reading before writing one, and each has a
  test. **The GIL is not held across the fetch:** the design note that
  preceded this assumed a Python object on the fetch path would stop the
  process and proposed a worker thread and a channel, and the measurement
  says otherwise — a `fetch()` doing I/O releases the GIL itself, so a
  second thread keeps running at 68–102% of its free rate. The worker
  would also have created the one deadlock this shape does not have: a
  `fetch()` calling back into the extension would be waiting on the
  thread it runs on. **The timeout is the `fetch()` implementation's**,
  because nothing on the Rust side can interrupt Python that has decided
  not to return; a `KeyboardInterrupt` out of a fetch propagates
  unchanged rather than becoming a store failure. **A failing `fetch()`
  is reported, never fatal:** it arrives as `RemoteError` — or
  `AuthError`, if that is what was raised, so *this credential was
  refused* stays distinguishable from *the store is unreachable* — with
  the original attached as `__cause__` and its message deliberately not
  repeated, because a store's exception routinely carries the URL it
  called. The previous document, the previous model and the
  last-known-good cache are all untouched by one.

  `describe()` is asked once, when the source is installed, because the
  engine reads it on the load path and a load must not re-enter Python.
- **A free-threading audit, as a book page**
  ([Free-Threaded CPython](https://dynamic-config-rs.github.io/python/free-threading.html)).
  Every `static`, every `#[pyclass]`, and every place correctness rode on
  the GIL without saying so. Two fixes came out of it, both below.
  **Nothing has been run on a free-threaded interpreter**, so the module
  still ships without `Py_mod_gil = Py_MOD_GIL_NOT_USED` — a
  free-threaded CPython re-enables the GIL when it imports this, which is
  the safe default and stays until the suite has actually run without
  one. The page says what remains, including the abi3 blocker: a
  `Py_GIL_DISABLED` build has no stable ABI, so supporting it means a
  second, non-abi3 wheel per platform.
- **`config.overrides(**values)`, the override layer scoped to a block.**
  The shape a test wants, because the long hand ends in a cleanup step
  that is easy to forget — and a forgotten `clear_overrides()` leaks into
  the next test through whatever configuration the module built:

      with config.overrides(pool_size=1, host="localhost"):
          ...        # reloaded on entry, and again on the way out

  The exit restores the override layer the block *found* rather than
  emptying it, so a nested `with` composes and a pin set before the block
  still stands after it; it restores on an exception too, so a failing
  assertion does not decide what the next test sees. Dotted paths are
  spelled with `__` — `pool__max_size=1` — the same nesting rule the
  environment layer already teaches.
- **A pytest plugin ships with the package**, found through a `pytest11`
  entry point, so installing `dynamic-config-py` is the whole setup.
  `dynamic_config_workspace` is a temporary directory that is also the
  working directory, so a relative `file("app.toml")` finds this test's
  copy; `dynamic_config_env("APP_")` unsets the variables a developer's
  shell would otherwise contribute. Neither is autouse — a plugin that
  arrives with the wheel should not change what a test sees until the
  test asks. `dynamic_config.pytest` imports pytest and the standard
  library and **nothing else**, Pydantic included: it is loaded in every
  pytest run of every environment this package is installed in, so a
  dependency there would be a dependency for all of them. This binding's
  own suite runs on those two fixtures, which is how the thing that ships
  stays the thing that is tested.
- **`repr(config)` carries the generation** —
  `<DynamicConfig Database key='db' generation=3>` — which is what a
  debugger session wants, and `generation=0` says nothing has installed
  yet. Still shape-only: no values, ever.
- **Pydantic is optional, and a `dataclasses.dataclass` is a schema.**
  The base install has no dependencies at all — the engine is compiled
  into the wheel, and the stdlib already has a way to declare a record:

      pip install dynamic-config-py                     dataclasses
      pip install dynamic-config-py[pydantic]           + models
      pip install dynamic-config-py[pydantic-settings]  + BaseSettings
      pip install dynamic-config-py[all]                all of it

  Importing the package with Pydantic uninstalled loads no Pydantic
  module, and CI asserts exactly that in a bare virtualenv. Everything
  else is the same object either way — sources, precedence, watching,
  recovery, the diagnostics, the redacted cache — and what changes is
  what validation *means*. A dataclass is validated structurally:
  required fields present, no key the class never declared, nested
  dataclasses built recursively, and each value against its declared
  type, with `bool` and `int` kept apart. It does not coerce, except
  where a type parses its own text — an `Enum` takes its member's value,
  `date`/`time`/`datetime` go through `fromisoformat`, and `UUID`,
  `Path`, `Decimal` and `IPv4Address` build from theirs. Secrets are
  declared the stdlib's way, `field(metadata={"secret": True})`, and
  drive the same redaction a `SecretStr` does.
- **`init_and_current()`**, and `init_and_current_async()`, for the two
  calls that always pair. Starting up is the one moment a program wants
  both, and writing it as two statements means naming the configuration
  twice::

      db = DynamicConfig(Database, key="db").file("app.toml").init_and_current()

- **This package versions independently of the Rust crates.** It embeds
  the engine rather than depending on a published version of it, so a
  Rust-only release is not a reason to ask every Python user to upgrade.
  `dynamic_config.__version__` is this package; `__engine_version__` is
  the `dynamic-config` crate the wheel was built against.
- The first release: a PyO3 extension pairing the `dynamic-config` engine
  with Pydantic validation.
  - `DynamicConfig(Model, key=..)` with the whole source surface —
    `file`, `discover`, `env`, `nest`, `allow_empty_env`, `strict_env`,
    `env_file`, `profile_env`, `cache` — and the whole lifecycle:
    `init`/`init_async`, `load`/`load_async`, `reload`, `current`/
    `try_current`, `replace`, `watch`, `on_reload`, `changed`, and
    `changes()` as an async iterator.
  - The runtime layers (`set_default`, `set_defaults`, `set_override`,
    `set_assignments`, `clear_*`), `alias` and `bind_env`.
  - The diagnostics: `source_of`, `is_set`, `explain`, `check`,
    `snapshot`.
  - The `@dynamic_config(...)` decorator, which attaches a configuration
    to a model class without loading at import time.
- **Validation once per resolve, never per read.** A reload Pydantic
  rejects keeps the previous model serving and writes no cache, exactly
  as a Rust `validate` refusal does.
- **Secrets derived from the model**: `SecretStr` and `SecretBytes` —
  through `Optional`, unions, containers, nested models, Pydantic
  dataclasses and `RootModel` — seed the redaction the cache, `explain`
  and the scrubbed `ValidationError` all follow. A field contributes
  **every** name a file could carry it under: each alias shape Pydantic
  accepts (`AliasChoices`, `AliasPath`, `alias`, an `alias_generator`)
  and the field name, because a secret spelled the other way is still a
  secret. Over-listing costs a key nothing supplies; under-listing put a
  password in `explain` and in the "redacted" cache on disk.
- **`pydantic-settings` is supported as a schema, and translated as a
  declaration.** A `BaseSettings` class works here as any model does, and
  `DynamicConfig.from_settings(Settings, key=..)` reads its
  `SettingsConfigDict` and rebuilds it as engine sources:
  `toml_file`/`json_file`/`yaml_file` become files, `env_file` becomes
  the dotenv layer, and `env_prefix` becomes one binding per leaf field,
  so `APP_PORT` stays `APP_PORT` rather than becoming `APP_<KEY>_PORT`
  and a deployment's existing variables keep working.
  `env_nested_delimiter` and `case_sensitive` shape those names. What has
  no engine equivalent is refused at the call rather than dropped —
  `secrets_dir`, `cli_parse_args`, an overridden
  `settings_customise_sources`. Constructing a `DynamicConfig` directly
  from a class that declares sourcing warns: being the source is a fine
  thing to choose, and believing an `env_prefix` is doing something is
  not.
- **Whatever a Pydantic model may be, it may be a schema here.**
  Inheritance to any depth, mixins, `model_config` (`extra`, `frozen`,
  `populate_by_name`, `alias_generator`, `validate_assignment`), field
  and model validators in both modes, computed fields, private
  attributes, `RootModel`, Pydantic dataclasses, generic models and
  discriminated unions — each with a test in `tests/test_pydantic.py`.
- Type stubs and a `Generic[M]` facade, so `current()` type-checks as the
  caller's model; `mypy --strict` runs over it in CI.
- An exception hierarchy mirroring `ErrorKind`, each instance carrying
  `kind`, `path` and `origin`.
- `changed_paths(previous, current)` — the audit half of a reload from
  Python: which paths moved, never what they moved to, secrets included
  in the comparison and excluded from the answer.
- The read path is a Python attribute rather than a boundary crossing:
  28 ns against a module global's 20, with the two caches' agreement
  pinned by a test on every install path.
- `reload_async`, `changed_async(timeout=…)` and `watch_async` complete
  the async surface: every blocking call has a twin that runs off the
  loop, and waiting for one reload no longer means iterating for all of
  them. Cancelling either wait is noticed within a quarter second.
  `watch_async` exists because *starting* a watcher is not free even
  though the watcher itself is a thread: it resolves directories and
  registers each with the notification backend, which measures a
  fraction of a millisecond natively and single-digit milliseconds when
  `poll_interval` makes it scan a large directory first. `Watch.stop()`
  deliberately has no twin — it drops the backend and returns without
  joining the thread or draining a debounce window.
- `set_executor(pool)` and `DynamicConfig(..., executor=pool)` choose
  which thread pool pays for the blocking half of an async call — the
  Python-side twin of the Rust crate's `set_blocking_executor`. Waits
  stay on the loop's default executor, so several `changes()` iterators
  cannot starve a small pool of the reload they are waiting for.
- **Callbacks with the ergonomics they were missing.** `on_reload` is
  usable as a decorator, because the guard it returns forwards calls to
  the hook — decorating no longer rebinds the name to something you
  cannot call — and carries the function as `.hook`. `on_change(*paths)`
  is the filter almost every reload hook opened with, written once: it
  fires only when one of those paths actually moved (a path naming a
  table covers what is under it, and the first install always counts,
  so a hook that sets something up runs at startup). The comparison is
  `changed_paths`, so a changed secret is noticed without being printed.
  [Callbacks](https://dynamic-config-rs.github.io/python/callbacks.html)
  is the chapter; `tests/test_callbacks.py` pins the contract — what the
  arguments mean, that a read inside a hook sees the new model, that
  hooks run in registration order on the thread that reloaded, and that
  one raising does not stop the rest.
- Seventeen runnable examples in `examples/` — including FastAPI (both
  `async def` and `def` endpoints, a lifespan-owned watcher and a test
  override), Flask, Django, a three-file asyncio service, three
  decorated services on one event loop, pydantic-settings, and a
  dataclass schema with nothing installed at all — all run in CI, and `benchmarks/read_path.py` for the numbers.
- **Ruff, as the linter and the formatter**, over the package, the suite,
  the examples and the benchmarks — including `pydocstyle`, so "every
  public definition is documented" is a rule the gate keeps rather than a
  habit. Configured at the 3.9 floor, with PEP 604 (`X | None`)
  deliberately disabled: Pydantic evaluates a model's annotations when
  the class is built, so `from __future__ import annotations` does not
  make that syntax safe for a 3.9 user, and neither the type checker nor
  vermin would catch it.
- **The package is modular**: `_config`, `_decorator`, `_diagnostics`,
  `_errors`, `_executor`, `_lifetime`, `_schema` and `_settings`, with
  `__init__.py` as the public surface and nothing else. Every import a
  user writes is unchanged; `mypy --strict` now runs over all of it
  rather than over one file.
- An integration suite (`tests/test_integration.py`) that runs whole
  scenarios rather than single calls: a service starting, watching,
  reloading and shutting down with every layer in play; a bad edit
  arriving at a running service; a restart recovering from the cache;
  four threads reading while another reloads, asserting no read ever
  sees half an install; and the shipped FastAPI, Flask and Django
  examples driven the way a test suite drives them, so an example that
  rots fails the suite.

- **Free-threaded CPython 3.14t is supported, and audited rather than
  assumed.** The module declares `Py_mod_gil = Py_MOD_GIL_NOT_USED`
  (`#[pymodule(gil_used = false)]`), and a `cp314t` wheel per platform
  ships beside the abi3 one — `Py_GIL_DISABLED` has no stable ABI, so one
  wheel cannot cover both. CI's `python-free-threaded` job runs the whole
  suite on 3.14.0t plus ten further iterations of the threading, shutdown
  and free-threading tests, and asserts `sys._is_gil_enabled()` is false
  after importing the extension.

  **3.14t and not 3.13t**: PyO3 0.29 dropped 3.13t, following CPython,
  which promoted free-threading from experimental to supported in 3.14.

  The audit found no `static` and no `unsendable` `#[pyclass]`, measured
  two of its own predictions false — the hook lock is not held while hooks
  run, and the read path costs the same multiple of a plain attribute
  lookup with and without a GIL — and changed one thing: `_LIVE_CONFIGS`
  and `_LIVE_WATCHES` are now guarded by a lock, because `weakref.WeakSet`
  is only as atomic as the GIL makes it and a registry that dropped
  entries would leave watchers running into finalization. See the book's
  *Free-Threaded CPython* page for what a green suite still does not
  prove.

  `abi3` is now a **default** Cargo feature rather than a hard dependency
  feature, and the free-threaded wheel is built with
  `--no-default-features`: cargo features are additive, so nothing can turn
  abi3 *off* by being turned *on*. Ordinary builds are unaffected. What
  actually selects the ABI is the interpreter — `maturin build -i
  python3.14t` — and the feature exists so that pyo3 is never *asked* for
  abi3 rather than rescued by its compatibility fallback.

  The free-threaded wheels are **manylinux x86-64 and aarch64 only**. The
  macOS and Windows runners were not verified for a free-threaded
  interpreter; the release job asserts its own wheel tags, so widening it
  is a matter of running it once.

### Changed

- **Every compiled method now carries a docstring naming each of its
  parameters**, and the `Config` class documents its constructor
  arguments where `help()` shows them — a signature a developer had to
  guess at was a parameter they had to go and read the source for. The
  decorator's own docstring gained the same list, one row per keyword
  and the fluent call it stands for.

- **`whole_document()`, and `whole_document=True` on the decorator**: reads
  each document as this model's values, with no section header —
  `{"host": "0.0.0.0", "port": 8000}` and nothing above it. The key still
  names the environment prefix, the cache entry and the diagnostics, and
  `key=""` is allowed for a configuration with nothing to call itself,
  whose environment layer is then the prefix alone (`APP_PORT`).

  `examples/19_document_shape.py` runs it, together with the three
  questions next to it that the binding answers differently from the
  engine: a key the model does not declare is ignored by a `BaseModel`,
  refused under `extra="forbid"`, and **always** refused by a dataclass —
  which has no `extra` setting to consult, so the builder names the field
  it could not place.

- `changes()` says what it does: it yields the installed model once per
  wake, latest-wins, rather than one entry per install. `on_reload` is
  the surface that runs for every install.
- `Watch.detach()` records that it opts out of the `atexit` sweep — a
  detached watcher is no longer reachable, which is the trade the call
  exists to make.

### Fixed

- **The suite and the examples are held to the 3.9 floor by a test rather
  than by one CI row.** `from __future__ import annotations` makes every
  annotation a string, so `list[str] | None` in a model body compiles
  anywhere and fails at *class creation* on 3.9, where Pydantic resolves
  it and PEP 604 does not evaluate — which is a slow way to find out, on
  the one interpreter a laptop may not have. `tests/test_floor_syntax.py`
  parses both wheels' suites and this one's examples on whatever
  interpreter is running: no PEP 604 where something evaluates
  annotations, and nothing newer than 3.9's grammar anywhere.

Everything here came out of a review of the release branch, and each one
is now pinned by a test.

- **The engine object now implements `__clear__`.** It reported its edges
  from `__traverse__` and had no way to drop them, so a cycle running
  through it needed the closure's own `tp_clear` to break — which worked
  for hooks and would not have for a Python remote source. Both edges are
  dropped now, which is the shape `tp_traverse`/`tp_clear` are meant to
  come in.
- **The shutdown registries are guarded by a lock.** `_LIVE_CONFIGS` and
  `_LIVE_WATCHES` are `weakref.WeakSet`s mutated from every thread that
  builds a configuration, and a `WeakSet` is only as atomic as the GIL
  makes it. A registry that lost an entry would leave a watcher running
  into finalization — the crash the whole `atexit` sweep exists to
  prevent. Found by the free-threading audit; it is a latent bug on a
  free-threaded build rather than a live one today.
- **A secret inside a container of models was redacted nowhere.** A field
  like `users: list[Credentials]` with a `SecretStr` inside recorded
  `users.password`, while the real paths are `users.0.password` — which
  `touches_secret` cannot match and the cache's path remover cannot
  descend to, so the password reached the *redacted* cache on disk. The
  containing field is redacted whole now: wrong in the direction that
  costs a reader some context rather than the one that costs a password.
- **A `RootModel` wrapping a model built the wrong path.** For
  `RootModel[Credentials]` the walk descended through the synthetic
  `root` field and produced `credentials.root.password`, but a file
  writes `credentials.password` — so nothing was redacted, in `explain`
  or in the cache. The root annotation is now walked at the outer path,
  which is what the comment beside it always claimed.
- **A validator's own message travelled into the diagnostics.** Pydantic
  puts the text of a custom `raise ValueError(...)` in `msg`, and
  `raise ValueError(f"invalid token {value}")` is the ordinary way to
  write one — so the value reached `str(InvalidError)` and `.errors`.
  Messages under `value_error` and `assertion_error` are replaced; the
  path and the type are kept, and Pydantic's own messages (value-free by
  construction) are untouched.
- **`from_settings` read no environment at all without an `env_prefix`.**
  `BaseSettings` reads `HOST` and `PORT` whether or not a prefix is
  declared, and that is the common shape; the binding loop only ran when
  a prefix existed, so a file or a default quietly won over a variable
  that pydantic-settings would have preferred.
- **`snapshot().to_dict()` rounded a large integer.** A `u64` above
  `i64::MAX` reached the installed model exactly and the exported
  snapshot as a float, so the two public views of one snapshot
  disagreed — against a promise the book makes in as many words.
- **`Literal` was unchecked in a dataclass schema.** `mode: Literal["read",
  "write"]` accepted `"delete"`, because a `Literal` reaches the
  container branch and fell through it. It is checked against its values
  now, with `True` and `1` kept apart.
- **`set_defaults(model)` silently dropped aliased fields.** The instance
  was dumped by field name, so a model whose field carries
  `alias="VALUE"` round-tripped to a key its own class does not accept —
  and the default vanished rather than raising. Dumped by alias now,
  which also lines `changed_paths` up with the paths `explain` and the
  secret list already use.
- **A reload hook that captured its configuration leaked it.**
  `@config.on_reload` closures reading `config.current()` — the
  documented idiom — close a cycle through the Rust `Config`, which had
  no `tp_traverse`, so Python's collector could not see the edge and the
  configuration, its models and its leaked layers lived until the
  process exited. `Config.__traverse__` closes it.
- **`load()` could return another thread's candidate.** The GIL is
  released for the resolve, so a concurrent load or a watcher-driven
  reload could stage its own model in the window; the returned model is
  now matched against the tree this call resolved.
- **A reload could be committed twice.** The staged sequence was read and
  stored as two steps, so the two commit paths for one install could both
  win — one reload, two generations, every hook run twice. The claim is a
  single `fetch_max` now. The GIL happened to serialise it; the invariant
  no longer depends on that.

- **`secrets_dir(path)`** — a directory where each file is one key, which
  is how Docker and Kubernetes mount credentials. `from_settings`
  translates a settings class's `secrets_dir` onto it, so the one
  translation that used to be refused is refused no longer. Provenance
  names the individual file, which is more than pydantic-settings can
  say. Values arrive as strings deliberately: a credentials directory is
  the worst place to guess that `12345` was meant as a number.
- **`Configured`, the mixin that makes the decorator type-check.** The
  decorator attaches six members at runtime, and no type checker can see
  that — `Database.current()` was an `attr-defined` error under
  `mypy --strict` and offered no completion in an editor, while running
  perfectly. Inheriting `Configured` declares them where a checker looks;
  the decorator fills them in, `model_fields` is untouched, and runtime
  behaviour is identical. The plain decorator still works and is not
  deprecated — it simply cannot be made visible to a checker.
  `tests/typing/usage.py` now runs under `mypy --strict` in CI, checking
  a file written the way a *caller* writes one, because types that
  regress for a user are invisible to a test suite.

### Security

- Pydantic's `ValidationError` echoes the offending input by default;
  across this boundary it is scrubbed to locations, messages and error
  types, and attached as `InvalidError.errors`.
- Every object's `repr` shows shape rather than values, and a watcher is
  stopped at interpreter shutdown rather than left to call into a
  finalized Python.

