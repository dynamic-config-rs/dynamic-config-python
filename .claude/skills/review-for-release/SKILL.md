---
name: review-for-release
description: Use before cutting a release, or when asked to review the whole repository — the checks that have actually caught things here, in the order that finds problems fastest.
---

# Reviewing before a release

Run `just check` first. Everything below is what that does not catch.

## The checks that have caught real bugs here

**Run the suite twice: parallel, then `--test-threads=1`.** The macro takes
literal paths, and layers, aliases and bindings live in `static`s — two tests
sharing a config type, a fixture path or an environment variable race in
parallel and pass alone. Both orders have found bugs.

**Grep for `Instant::now() +` and `Duration::from_*` on anything a server
sent.** A lease duration from a compromised or buggy server panics the process
on the arithmetic. `checked_add`, always.

**Check every error path that removes a file.** `save_new` deleted the file it
had just refused to overwrite. Ask: is this call what created the thing being
cleaned up?

**Read every `.expect(` and `panic!` outside tests.** The crate's stance is that
a poisoned lock recovers rather than propagating: none of the data behind those
locks has an invariant a panic could break, and one panicking caller must not
take out every later load.

**Look for values in diagnostics.** Diffs, `check()` reports, unknown-key
suggestions and *error messages* report paths and types, never values.
`tests/security.rs` enforces it; a new message that interpolates a value is a
security regression that no test names.

**Verify MSRV against real toolchains.** `age` declares 1.74 and needs 1.85.
A manifest is a claim, not a measurement. `just msrv` runs every floor.

**Check CI parity.** The interpreter matrix in ci.yml, the wheel matrix in
release.yml, `requires-python` and the classifiers all name versions —
and the free-threaded job is the only one that proves the GIL is actually
off. Diff them, don't skim them.

**Audit the stacked `#[cfg]`s.** Two `#[cfg]` attributes on one item AND
together — `#[cfg(unix)] #[cfg(not(unix))]` compiles to nothing, silently.
Three tests here never ran for months because of one. Grep for consecutive
cfg lines and read each pair.

**Check the counts.** Two wheels, eight ci.yml jobs, twenty-two examples,
and the interpreter list in three places: `pyproject.toml`'s classifiers,
the README's table and the book. PyPI filters on the classifiers, and a
classifier nobody tested is a claim.

**`cargo clippy -- -W clippy::pedantic`** for the substantive lints only:
`unnecessary_wraps`, `needless_pass_by_value` on public API, `redundant_clone`,
`must_use_candidate`. Ignore the style noise.

## Documentation

- Every anchor and relative link resolves. A dead `#anchor` in a README is
  small, easy to ship, and the first thing a reader hits.
- Counts anywhere in the documentation — tests, examples, features, crates —
  match reality. Run the suite and count rather than trusting the last number.
- The book's example output matches what the examples print. Run them.
- `python -m pytest`, `mypy --strict` and `ruff` all clean, and every public
  definition carries a docstring — `help()` is this package's manual.
- The facade, `_core.pyi` and `book/src/reference.md` agree. Nothing else
  checks that.
- Each crate's README is *its own*, not the repository's.

## Release mechanics

- Both wheels carry the same version, and the remote wheel's
  `dynamic-config-py>=…` floor names it. `./scripts/release-python.sh
  --check` proves all three.
- The version is not already on PyPI — `maturin upload --skip-existing`
  makes a repeat a silent no-op, which is how a whole wave once published
  nothing.
- Never run `maturin upload`. Merging into `main` publishes.
