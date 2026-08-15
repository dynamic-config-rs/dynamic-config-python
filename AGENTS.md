# AGENTS.md

Two wheels: `dynamic-config-py`, and `dynamic-config-py-remote` behind the
`[remote]` extra. Both are PyO3 extensions around
[the engine](https://github.com/dynamic-config-rs/dynamic-config), which is
a crates.io dependency here, not a sibling.

## Orientation

```text
dynamic-config-python/
  src/                     the compiled surface: PyO3, one module per concern
  python/dynamic_config/   the facade — what a caller imports, fully documented
    _core.pyi              the stub; mypy --strict sees through this, or nothing
  tests/                   behaviour, not calls
  examples/                twenty-two runnable scripts; CI runs every one
dynamic-config-python-remote/
  src/                     the eight stores, wrapped
  python/dynamic_config_remote/
```

**Both wheels version together, always.** The extra resolves to a *pair*,
and the remote wheel imports `Format` and `RemoteSource` from the base one
— a gap between them is a combination nobody has tested.
`scripts/release-python.sh --check` is what proves the two manifests and
the floor in `pyproject.toml` still agree.

## Commands

```sh
just check            # fmt, clippy, both wheels' suites. Needs a venv with maturin
just python           # the base wheel: pytest, mypy --strict, ruff, examples
just python-remote    # the stores wheel; needs `just python` to have run first
just python-free-threaded /path/to/venv   # 3.14t, with the GIL actually off
just book             # this repository's book
```

Skills in `.claude/skills/`: [changing the
bindings](.claude/skills/change-python-bindings/SKILL.md), [triaging the
security tab](.claude/skills/triage-security/SKILL.md), [reviewing before a
release](.claude/skills/review-for-release/SKILL.md). There is one
subagent, [`python-binding-reviewer`](.claude/agents/python-binding-reviewer.md),
for the invariants whose failures are silent — validation moving after the
install, a read crossing back into Rust.

## Rules that are not negotiable

**Two halves that mirror each other, and nothing enforces it.** A change to
the compiled surface has to reach the facade (with a docstring — `help()`
is this package's manual, and ruff's `pydocstyle` fails the gate without
one), `_core.pyi`, `book/src/reference.md` and the pytest suite.
`.claude/hooks/binding-drift.sh` prints that list while the change is still
in hand.

**Secrets are paths and types, never values.** Diffs, `check()` reports,
`explain` and *error messages* say which key moved and what was expected —
never what was there. That includes messages a schema library wrote:
msgspec quotes the refused value in two of its messages, and the adapter
takes it back out.

**A secret under a container redacts the containing field.** A dotted path
cannot index a list, so `users.password` names nothing the redaction can
walk to. Losing the usernames from a cache costs a diagnostic; keeping the
passwords in one costs rather more.

**Validation happens before the install, on the loading thread.** A
document the schema refuses installs nothing and leaves the previous one
serving — from the watcher exactly as from an explicit reload. Anything
that moves validation after the swap breaks the property this binding
exists for.

**The GIL is not a lock this code may rely on.** The free-threaded wheel is
built and tested with it off; a `static mut`, a lazily-initialised global
or a borrow held across a call into Python is a bug there even when it
passes everywhere else.

## Mistakes this repository has actually seen

**Believing a manifest.** Measure the floor against a real toolchain, then
write the number down.

**A silent no-op release.** `dynamic-config-py` 0.1.0 shipped, and the next
wave prepared 0.1.0 again — `maturin upload --skip-existing` published
nothing at all and said so quietly. `--check` refuses a version already on
PyPI for exactly this reason.

**Tests that share state.** One configuration key, one fixture path, one
environment variable per test. Two tests sharing any of the three race, and
pass alone.

## What a change must carry

The facade, the stub, the chapter, a test, and an entry under
`## [Unreleased]` in `dynamic-config-python/CHANGELOG.md` — the remote
wheel's own changelog when that is what moved.

## Releasing

Do not publish. `scripts/release-python.sh patch` prepares both wheels;
merging the bump into `main` is what publishes. See
[RELEASING.md](RELEASING.md), and never run `maturin upload` directly.
