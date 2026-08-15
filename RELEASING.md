# Releasing

Two wheels, one version, published together — `dynamic-config-py` and
`dynamic-config-py-remote`. Nothing here goes to crates.io.

**Two distributions, one version, always.** `dynamic-config-py[remote]`
resolves to a *pair*: the remote wheel imports `Format` and `RemoteSource`
from the base one, so a gap between them is a combination nobody has
tested. The cost is stated rather than hidden — a fix to the etcd binding
bumps the base wheel too, and its changelog will carry a version whose
entry says nothing changed there.

`scripts/release-python.sh` moves all five files in one commit: both
manifests, both changelogs, and the `dynamic-config-py>=…` floor in the
remote wheel's `pyproject.toml`, which lags into a broken pair if it is
left behind.

## The branch model

Work lands on `dev`. `main` is production: it accepts no direct pushes —
not even from admins — only pull requests whose gates ("CI is green",
"Security is green") have passed, merged with a linear history.

**Merging a version bump into `main` is the release.** There is no tag to
push by hand: `release.yml` runs on every push to `main`, checks whether
the wheel version is new, and — only then — builds every wheel, uploads
them, and mints the tag and the GitHub release itself, at the merge commit.

## The lifecycle, step by step

1. **Land the work on `dev`** through pull requests, entries accumulating
   under `## [Unreleased]` in the changelog of whichever wheel changed.
2. **Pre-flight.** `just check` on `dev`, and
   `just python-free-threaded /path/to/venv` when anything touched
   threading, the GIL, or a global.
3. **`./scripts/release-python.sh --check`.** It refuses a version that is
   already on PyPI — and that is not hypothetical: 0.1.0 shipped with one
   release, the next wave prepared 0.1.0 again, and `maturin upload
   --skip-existing` made the whole wave a silent no-op. It also checks that
   the two manifests agree, that the floor matches, and that there is
   something under `## [Unreleased]` to release.
4. **`./scripts/release-python.sh patch`** (or `minor`; pre-1.0 a breaking
   change is `minor`). Bumps both, rotates both changelogs, moves the
   floor, and makes one local commit — no push, no tag, no publish.
5. **`cargo check -p dynamic-config-python`**, then
   `git add Cargo.lock && git commit --amend --no-edit`: the lockfile
   follows the bump, and a release commit that leaves it behind makes the
   next `cargo` command dirty the tree.
6. **Read the commit.** `git show --stat HEAD`: two manifests, two
   changelogs, one `pyproject.toml`, one lockfile. Exactly one heading per
   version, entries under the new one, `Unreleased` empty again.
7. **`./scripts/promote.sh`.** Pushes `dev`, opens or updates the pull
   request, arms auto-merge and waits; when both gates pass, the
   squash-merge lands — **that merge is the release**.
8. **`./scripts/watch-release.sh`.** Follows the run the merge set off: a
   wheel per platform, the free-threaded pair, `maturin upload
   --skip-existing`, then the tag and the GitHub release.

A Python-only release needs nothing else. `./scripts/release-python.sh
--publish` exists for the case where a wheel has to go out from a commit
that is already on `main` — it dispatches the same workflow by hand.

## What an operator has to have ready

`PYPI_TOKEN` as a repository secret, with upload rights to both projects.
Nothing else: the wheels are built by `maturin-action` on GitHub's own
runners, and the token is the only thing that is not in this repository.

## Afterwards

Check that both projects show the new version on PyPI, that the classifiers
list the interpreters this release actually tested, and that
`pip install "dynamic-config-py[remote]"` in a clean environment resolves
to the pair rather than to one new wheel and one old one.

## Version policy

- **Pre-1.0, a breaking change bumps the minor version** and everything
  else the patch.
- A change to the minimum supported Python version is breaking. So is
  dropping a platform wheel.
- MSRV changes are breaking for anybody building from source, and every
  floor has a CI row against a real toolchain.
- The engine is a dependency here, named with a caret. A breaking engine
  release is not automatically a breaking release of these wheels — what
  matters is whether the *Python* surface moved.
