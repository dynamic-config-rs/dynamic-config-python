# Contributing

New here? [docs/CONTRIBUTOR-ONBOARDING.md](https://github.com/dynamic-config-rs/dynamic-config/blob/main/docs/CONTRIBUTOR-ONBOARDING.md) is a
tour of every crate and module — what each does, why it is shaped that way, and
where you would touch it. This file is the short version.

## Branches

Pull requests target **`dev`**. `main` is the default branch — the one
visitors land on — and it is production: nothing lands there except `dev`
promotions that passed every gate (squash-merged, one commit per
promotion), and releases are tags on it.

## Before code

For anything larger than a fix, open an issue first. Not for permission — to
find out whether the thing has already been decided against, and why.
[Not planned](https://dynamic-config-rs.github.io/limitations.html#not-planned) records what was refused and why, and
[ROADMAP.md](https://github.com/dynamic-config-rs/dynamic-config/blob/main/ROADMAP.md) what might still be built. Both are shorter than a
list of what exists, and more useful.

## Running everything

```sh
just check              # both wheels: pytest, mypy --strict, ruff, examples
```

It needs a virtual environment with maturin in it:

```sh
uv venv && uv pip install maturin pytest pytest-asyncio pydantic msgspec mypy ruff
```

`scripts/` holds the flows around the checks; see
[scripts/README.md](scripts/README.md).

**`just python` before `just python-remote`.** The remote wheel's tests
import the base package, so a stale base install fails as
`no attribute 'EtcdStore'` — a long way from the cause.

**The free-threaded build is a second interpreter** and needs its own
environment:

```sh
uv venv --python 3.14t /tmp/ft
VIRTUAL_ENV=/tmp/ft uv pip install maturin pytest pytest-asyncio pydantic
just python-free-threaded /tmp/ft
```

## The Python bindings, without a GIL

`just python` runs the suite on whatever interpreter the venv holds. The
free-threaded build is a second one, and it needs its own venv because the
wheel is not abi3:

```sh
uv python install 3.14t
uv venv --python 3.14t /tmp/ft && VIRTUAL_ENV=/tmp/ft uv pip install maturin pytest pytest-asyncio pydantic
cd dynamic-config-python && VIRTUAL_ENV=/tmp/ft maturin develop --no-default-features
VIRTUAL_ENV=/tmp/ft /tmp/ft/bin/python -m pytest tests -q
```

`maturin develop` uses the active venv, so it picks the free-threaded
interpreter and emits a `cp314t` build. `maturin **build**` does not — without
`-i` it builds an abi3 wheel against no interpreter in particular and ignores
the venv entirely, so CI passes `-i python3.14t` and that flag is the
load-bearing one. `--no-default-features` switches off the `abi3` Cargo
feature as well; it does not change the tag, but it means pyo3 is never asked
for abi3 and the build does not lean on pyo3's backward-compatibility fallback.
Cargo features are additive, so abi3 has to be a default that is dropped rather
than an opt-in. 3.14t and not 3.13t: PyO3 0.29 dropped 3.13t, following
CPython.

Two things are worth knowing before changing anything there. PyO3 has declared
modules GIL-free *by default* since 0.28, so a module that says nothing is
already making the claim — `src/lib.rs` writes `gil_used = false` out anyway,
so the claim lives where it is made. And
`tests/test_free_threaded.py::test_the_module_declares_itself_gil_free` asserts
`sys._is_gil_enabled()` rather than watching for the interpreter's warning: the
warning fires once per process at the first import, so a test that reloads the
module and catches warnings passes either way.
[Free-Threaded CPython](book/src/free-threading.md) is the audit.

## What a change should carry

**A test that would fail without it.** Not a test that exercises the new code —
one that catches the bug coming back.

**The reasoning, where it is not obvious.** Comments here explain *why*, not
what: the code says what. If you chose between two reasonable designs, the
rejected one belongs in a comment or in the roadmap.

**Documentation, if a user would notice.** A new macro argument goes in the
README's attribute table with a section of its own; a new feature goes in the
feature table and, if it moves the floor, the MSRV table.

**A changelog entry**, under `Unreleased`.

## Things that are load-bearing

Changing any of these is fine — arguing for it is the price:

- **Reading is lock-free.** `current()` is an atomic load and nothing more.
- **`figment` is the loader and does not appear in a signature.** A figment
  major bump should not be a breaking change here.
- **Secrets are paths, never values.** Every diagnostic reports which key moved,
  not what it moved to.
- **The core crate's MSRV is 1.71**, and every feature that raises it says so in
  the README table. Features that raise it are verified against real toolchains
  in CI, not trusted from a manifest — `age` declares 1.74 and needs 1.85.
- **No mandatory dependency** beyond `serde`, `arc-swap` and the engine's
  default resolution backend.

## Style

`rustfmt` decides layout; `clippy` with `-D warnings` decides the rest. Beyond
that: name things after what they mean to a caller, and let comments carry the
decisions rather than the mechanics.
