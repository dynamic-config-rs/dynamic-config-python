#!/usr/bin/env bash
# The Python packages' release, which is not the workspace's.
#
#   scripts/release-python.sh patch|minor|major|<version>   # prepare
#   scripts/release-python.sh --check patch|minor|<version> # would it work?
#   scripts/release-python.sh --publish                     # after it lands
#   scripts/release-python.sh --status                      # what is where
#
# `dynamic-config-py` versions independently of the crates: the wheel
# embeds the engine rather than depending on a published version of it, so
# a Rust-only release has nothing in it for a Python user, and a Python-only
# fix should not drag ten crates behind it. `cargo release` skips both
# packages for exactly that reason — which leaves the steps it would
# otherwise do, and this script is those steps.
#
# **Two distributions, one version.** `dynamic-config-py-remote` is the
# second wheel — `dynamic-config-py[remote]` resolves to it — and it moves
# with the first, always. They are built from one commit by one job, the
# extra resolves to a *pair*, and CI asserts the two manifests agree; a gap
# between them is a resolution nobody has tested, where an older base wheel
# meets a remote wheel that imports from it. The cost is stated rather than
# hidden: a fix to the etcd binding bumps the base wheel too.
#
# What it does *not* do: push, tag, or publish. Publishing is CI's, after
# the gates. The split is the same one the workspace release keeps.
set -euo pipefail
cd "$(dirname "$0")/.."

# distribution → cargo manifest · changelog · PyPI name
base_manifest="dynamic-config-python/Cargo.toml"
base_changelog="dynamic-config-python/CHANGELOG.md"
base_pypi="dynamic-config-py"

remote_manifest="dynamic-config-python-remote/Cargo.toml"
remote_changelog="dynamic-config-python-remote/CHANGELOG.md"
remote_pyproject="dynamic-config-python-remote/pyproject.toml"
remote_pypi="dynamic-config-py-remote"

version_of() {
    # The first `version` under `[package]`, which is that crate's own.
    awk '/^\[package\]/ { in_package = 1; next }
         /^\[/ { in_package = 0 }
         in_package && /^version = / { gsub(/[",]/, "", $3); print $3; exit }' "$1"
}

current() { version_of "${base_manifest}"; }

# The floor the remote wheel puts on the base one, which has to be the
# version they are both at: a `>=` that lags lets pip pair a new remote
# wheel with an older base wheel, which is the one combination nobody built.
declared_floor() {
    sed -n 's/.*dynamic-config-py>=\([0-9][0-9.]*\).*/\1/p' "${remote_pyproject}" | head -1
}

published() {
    # The version PyPI has, or nothing at all.
    curl -fsSL "https://pypi.org/pypi/$1/json" 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null ||
        true
}

entries_under_unreleased() {
    awk '/^## \[Unreleased\]$/ { on = 1; next }
         /^## / { on = 0 }
         on && /^[-*] / { count++ }
         END { print count + 0 }' "$1"
}

bumped() {
    local version=$1 kind=$2
    local major minor patch
    IFS=. read -r major minor patch <<<"${version}"

    case "${kind}" in
        major) echo "$((major + 1)).0.0" ;;
        minor) echo "${major}.$((minor + 1)).0" ;;
        patch) echo "${major}.${minor}.$((patch + 1))" ;;
    esac
}

status() {
    echo "packages:"
    echo "  ${base_pypi}          $(current)   (${base_manifest})"
    echo "  ${remote_pypi}   $(version_of "${remote_manifest}")   (${remote_manifest})"
    echo "  the remote wheel requires ${base_pypi}>=$(declared_floor)"
    # The engine is a *dependency* here, not a member: this workspace
    # publishes wheels and `[workspace.package]` carries no version at all,
    # which is why reading one printed an empty string.
    echo "engine:   dynamic-config $(awk -F\" '/^\[workspace.dependencies\]/ { p = 1; next } /^\[/ { p = 0 } p && /^dynamic-config = .*version = / { print $2; exit }' Cargo.toml)"
    echo

    echo "changelogs:"
    echo "  ${base_changelog}: $(entries_under_unreleased "${base_changelog}") entr(y|ies) under Unreleased"
    echo "  ${remote_changelog}: $(entries_under_unreleased "${remote_changelog}") entr(y|ies) under Unreleased"
    echo

    echo "on PyPI:"
    for name in "${base_pypi}" "${remote_pypi}"; do
        local there
        there=$(published "${name}")
        echo "  ${name}: ${there:-not published yet}"
    done
}

# Everything that has to be true before a version can be prepared, checked
# without changing anything. Run on its own, and again as the first thing
# `prepare` does.
#
# The PyPI check is the one that earned its place: `dynamic-config-py` 0.1.0
# shipped with the 0.5 release, and the 0.6 wheel wave was a silent no-op —
# `maturin upload --skip-existing` skips a version that is already there —
# until somebody read the PyPI JSON by hand.
check() {
    local target=${1:-$(current)}
    local problems=0

    local base_version remote_version
    base_version=$(current)
    remote_version=$(version_of "${remote_manifest}")

    if [[ ${base_version} != "${remote_version}" ]]; then
        echo "✗ the two manifests disagree: ${base_version} and ${remote_version}" >&2
        echo "  They version together; CI asserts it too." >&2
        problems=$((problems + 1))
    fi

    if [[ $(declared_floor) != "${base_version}" ]]; then
        echo "✗ ${remote_pyproject} requires ${base_pypi}>=$(declared_floor), not ${base_version}" >&2
        echo "  A floor that lags lets pip pair this wheel with an older base one." >&2
        problems=$((problems + 1))
    fi

    for name in "${base_pypi}" "${remote_pypi}"; do
        local there
        there=$(published "${name}")

        if [[ -n ${there} && ${there} == "${target}" ]]; then
            echo "✗ ${name} ${target} is already on PyPI" >&2
            echo "  The wheel wave uploads with --skip-existing, so preparing" >&2
            echo "  this version again would publish nothing at all." >&2
            problems=$((problems + 1))
        fi
    done

    for file in "${base_changelog}" "${remote_changelog}"; do
        if ! grep -q "^## \[Unreleased\]$" "${file}"; then
            echo "✗ no '## [Unreleased]' heading in ${file}" >&2
            problems=$((problems + 1))
        fi
    done

    if [[ $(entries_under_unreleased "${base_changelog}") -eq 0 &&
          $(entries_under_unreleased "${remote_changelog}") -eq 0 ]]; then
        echo "✗ nothing under '## [Unreleased]' in either changelog" >&2
        echo "  A release with an empty section is a release nobody can read." >&2
        problems=$((problems + 1))
    fi

    if [[ ${problems} -eq 0 ]]; then
        echo "✓ ready to prepare ${target}"
        return 0
    fi

    return 1
}

publish() {
    local version
    version=$(current)

    echo "Dispatching the wheel wave for ${base_pypi} and ${remote_pypi} ${version}."
    echo
    echo "This builds a wheel per platform and uploads with --skip-existing,"
    echo "so a version already on PyPI is a no-op rather than an error — which"
    echo "is why '--check' refuses to prepare one."
    echo

    if [[ $(git rev-parse --abbrev-ref HEAD) != "main" ]]; then
        echo "note: you are not on main. The dispatch runs against main's"
        echo "      workflow file either way — make sure ${version} landed there."
        echo
    fi

    read -r -p "Dispatch release.yml? [y/N] " answer
    [[ ${answer,,} == y ]] || { echo "not dispatched."; exit 0; }

    gh workflow run release.yml --ref main
    echo
    echo "Watch it with: ./scripts/watch-release.sh"
}

prepare() {
    local requested=$1
    local from to
    from=$(current)

    case "${requested}" in
        major | minor | patch) to=$(bumped "${from}" "${requested}") ;;
        [0-9]*.[0-9]*.[0-9]*) to=${requested} ;;
        *)
            echo "usage: $0 patch|minor|major|<version>" >&2
            exit 2
            ;;
    esac

    if [[ -n $(git status --porcelain) ]]; then
        echo "the tree is dirty; commit or stash first" >&2
        exit 1
    fi

    check "${to}" || exit 1

    local entries
    entries=$(( $(entries_under_unreleased "${base_changelog}") +
                $(entries_under_unreleased "${remote_changelog}") ))

    echo
    echo "${base_pypi} and ${remote_pypi}: ${from} → ${to}, with ${entries} changelog entr(y|ies)."
    read -r -p "Prepare it? [y/N] " answer
    [[ ${answer,,} == y ]] || { echo "nothing changed."; exit 0; }

    # The version, in the two places it lives, plus the floor that has to
    # follow it.
    python3 - "${from}" "${to}" "${base_manifest}" "${remote_manifest}" "${remote_pyproject}" <<'PY'
import re
import sys

before, after = sys.argv[1], sys.argv[2]
base_manifest, remote_manifest, remote_pyproject = sys.argv[3], sys.argv[4], sys.argv[5]


def bump_manifest(path):
    """The `version` under `[package]`, and nothing else in the file."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    package = text.index("[package]")
    end = text.index("\n[", package + 1)
    head, body, tail = text[:package], text[package:end], text[end:]
    body, count = re.subn(rf'^version = "{re.escape(before)}"$', f'version = "{after}"',
                          body, count=1, flags=re.MULTILINE)

    if count != 1:
        raise SystemExit(f'{path}: no version = "{before}" under [package]')

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(head + body + tail)


bump_manifest(base_manifest)
bump_manifest(remote_manifest)

with open(remote_pyproject, encoding="utf-8") as handle:
    text = handle.read()

text, count = re.subn(rf"dynamic-config-py>={re.escape(before)}",
                      f"dynamic-config-py>={after}", text, count=1)

if count != 1:
    raise SystemExit(f"{remote_pyproject}: no dynamic-config-py>={before} to move")

with open(remote_pyproject, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

    # Both changelogs, rotated the way cargo-release rotates the others.
    python3 - "${to}" "${base_changelog}" "${remote_changelog}" <<'PY'
import datetime
import sys

version, paths = sys.argv[1], sys.argv[2:]
today = datetime.date.today().isoformat()
heading = "## [Unreleased]"

for path in paths:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    if heading not in text:
        raise SystemExit(f"{path}: no Unreleased heading")

    # Unbracketed, unlike the workspace changelogs: those bracket a version
    # because a link definition at the bottom resolves it to a tag, and
    # these packages have no tag of their own — the repository's tags are
    # workspace versions. A bracket with nothing behind it renders as
    # literal brackets.
    text = text.replace(heading, f"{heading}\n\n## {version} — {today}", 1)

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
PY

    git add "${base_manifest}" "${base_changelog}" \
            "${remote_manifest}" "${remote_changelog}" "${remote_pyproject}"
    git commit -m "release ${base_pypi} and ${remote_pypi} ${to}"

    echo
    echo "Prepared, not pushed. What is left:"
    echo
    echo "  1. cargo check -p dynamic-config-python   # the lockfile follows the bump"
    echo "  2. git add Cargo.lock && git commit --amend --no-edit"
    echo "  3. git push origin \$(git rev-parse --abbrev-ref HEAD)"
    echo "  4. ./scripts/promote.sh          # the PR, the gates, the merge"
    echo "  5. $0 --publish                  # the wheel wave, once it is on main"
    echo
    echo "Step 5 is separate because the workspace release publishes crates"
    echo "and this publishes wheels — the same merge can carry both, and a"
    echo "Python-only release has no crates to wait for."
}

case "${1:---status}" in
    --status) status ;;
    --check)
        # With a target, because the useful question is *would the next one
        # work* — and the version this repository is on has, by definition,
        # already been released. Bare `--check` answers about that one and
        # therefore always reports it as taken; `--check minor` answers
        # about the release being planned. `prepare` runs the same check on
        # the same target before it changes a file.
        case "${2:-}" in
            "") check ;;
            major | minor | patch) check "$(bumped "$(current)" "$2")" ;;
            [0-9]*.[0-9]*.[0-9]*) check "$2" ;;
            *)
                echo "usage: $0 --check [patch|minor|major|<version>]" >&2
                exit 2
                ;;
        esac
        ;;
    --publish) publish ;;
    -h | --help)
        sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) prepare "$1" ;;
esac
