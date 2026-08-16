# Stability & Production Use

**Beta. The engine's surface is finished for 0.x; the binding's is not
quite.**

`dynamic-config-py` and `dynamic-config-py-remote` are Beta, like every
crate and package in this organisation. What that promise covers is
precise, and it changed once:

- **The engine — sources, layering, validation, diagnostics — is
  settled.** No new sources, no new schema kinds, no new methods on the
  settled types. What ships there is a defect that produces a wrong
  answer, a security advisory, and documentation.
- **The binding's concurrency surface was not settled, and 0.2 says so.**
  `ConfigGroup`, `events()`, dispatched hooks and `AsyncRemoteSource`
  are additions to this package, made after using the 0.1 surface from
  an asyncio service and finding the seams. Additive, so nothing written
  against 0.1 changed meaning — but additions, not hotfixes, and calling
  them hotfixes would be a lie about what the version means.

Between 0.2 and 1.0 the intent is the earlier one again: security fixes,
hotfixes, documentation. If that changes a second time it will be
written here in the same place, rather than discovered in a diff.

## What that means for your program

**Pin the minor version and take patches automatically.**

```
dynamic-config-py ~= 0.2.0
```

A patch will not break you. Pre-1.0 a break bumps the minor, is called out
in [the changelog](https://github.com/dynamic-config-rs/dynamic-config-python/blob/main/dynamic-config-python/CHANGELOG.md),
and comes with what to change on your side — and there is no plan to spend
one before 1.0.

**The two wheels version together.** `dynamic-config-py[remote]` resolves
to a pair; a gap between them is a combination nobody has tested, which is
why CI asserts their versions agree and one command moves both.

**The engine's version is a separate number.** `dynamic_config.__version__`
is this package's; `__engine_version__` is the Rust crate it was built
against. They move on two schedules, because a Rust-only release has
nothing in it for a Python user.

## Python versions

| Line | Status | Tested in CI | Notes |
|---|---|---|---|
| 3.9 | **supported** — the floor | ✅ every commit | The `requires-python` floor. `X \| None` is a syntax error here, which is why a test parses every file at this level |
| 3.10 | supported | ✅ every commit | |
| 3.11 | supported | ✅ every commit | `asyncio.timeout` exists from here; the binding does not depend on it |
| 3.12 | supported | ✅ every commit | |
| 3.13 | supported | ✅ every commit | |
| 3.14 | supported | ✅ every commit | |
| 3.14t | supported | ✅ every commit | Free-threaded, its own `cp314t` wheel — the concurrency suite ten times over |
| 3.8 and older | **not supported** | — | End of life; the wheel's `requires-python` refuses to install |

**One abi3 wheel per platform covers 3.9 upwards**, which is why the table
is short and why a new CPython does not need a new wheel. The matrix is
what makes the claim true rather than plausible: the *Python* half is
ordinary code, and a version can break it — `asyncio.timeout` arrived in
3.11, dataclass slots in 3.10, and the typing syntax the stubs use has
moved twice.

**Raising the floor is a breaking change**, treated exactly as an API
break: it bumps the minor and is called out in the changelog. It will not
happen before 1.0.

| Platform | x86-64 | aarch64 |
|---|---|---|
| Linux (manylinux 2_28) | ✅ | ✅ |
| macOS | ✅ | ✅ |
| Windows | ✅ | — |

## What is tested, and where you can see it

The claim behind Beta is evidence rather than time:

| | |
|---|---|
| The suite | 344 tests, on CPython 3.9 through 3.14 — every version the wheel claims, not just the newest |
| Free-threaded CPython | a `cp314t` wheel, with the concurrency suite run ten times over on a real no-GIL build |
| The base install | a job with the wheel and nothing else, proving `pip install dynamic-config-py` pulls in no schema library |
| Every example | all twenty-two run in CI; an example that only imports is not an example |
| The engine underneath | the Rust crate's own suite, its property tests, `loom` and `shuttle` models for the reload path, and instruction-count gates |
| The stores | each against a real server in a container, and three of them unplugged mid-watch by a proxy |

## What running this in production actually asks of you

**Decide what a failed reload should do.** The default is the right one
for most services — the previous configuration keeps serving and the
failure is recorded — but *recorded* means somebody has to look. Wire
`status()` into a health endpoint, or `config.on_reload` into whatever you
alert on. The
[telemetry chapter](telemetry.md) has the two numbers that matter:
`consecutive_failures`, and how old the serving document is.

**Give the last-known-good cache a path that survives a restart.** A
`redacted` cache means a broken source at startup is a warning rather than
an outage — and it refuses to write at all unless the configuration has
said what is secret, which is the point.

**Watch the watcher.** A file watcher is not a promise that a file will be
watched: a container bind mount and some network filesystems deliver no
events, and `poll_interval` is the answer there rather than a mystery.

**Nothing here needs a sidecar, an agent or a server.** The engine is in
your process, the reads are lock-free, and the only thing that leaves is
what a remote store you configured goes to fetch.
