#!/usr/bin/env bash
# The Python package's release, which is not the workspace's.
#
#   scripts/release-python.sh patch|minor|major|<version>   # prepare
#   scripts/release-python.sh --publish                     # after it lands
#   scripts/release-python.sh --status                      # what is where
#
# `dynamic-config-py` versions independently of the crates: the wheel
# embeds the engine rather than depending on a published version of it, so
# a Rust-only release has nothing in it for a Python user, and a Python-only
# fix should not drag ten crates behind it. `cargo release` skips this
# package for exactly that reason — which leaves the two steps it would
# otherwise do, and this script is those two steps.
#
# What it does *not* do: push, tag, or publish. Publishing is CI's, after
# the gates. The split is the same one the workspace release keeps.
set -euo pipefail
cd "$(dirname "$0")/.."

manifest="dynamic-config-python/Cargo.toml"
changelog="dynamic-config-python/CHANGELOG.md"

current() {
    # The first `version` under `[package]`, which is this crate's own.
    awk '/^\[package\]/ { in_package = 1; next }
         /^\[/ { in_package = 0 }
         in_package && /^version = / { gsub(/[",]/, "", $3); print $3; exit }' "${manifest}"
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
    echo "package:  dynamic-config-py $(current)   (${manifest})"
    echo "engine:   dynamic-config $(awk '/^\[workspace.package\]/ { p = 1; next } /^\[/ { p = 0 } p && /^version = / { gsub(/[",]/, "", $3); print $3; exit }' Cargo.toml)"
    echo

    if grep -q "^## \[Unreleased\]$" "${changelog}"; then
        local lines
        lines=$(awk '/^## \[Unreleased\]$/ { on = 1; next } /^## \[/ { on = 0 } on && NF' "${changelog}" | wc -l)
        echo "changelog: ${lines} line(s) under Unreleased"
    fi

    echo
    echo "on PyPI:"
    if command -v curl >/dev/null; then
        curl -fsSL https://pypi.org/pypi/dynamic-config-py/json 2>/dev/null |
            python3 -c 'import json,sys; print("  " + json.load(sys.stdin)["info"]["version"])' 2>/dev/null ||
            echo "  not published yet"
    else
        echo "  (curl not available)"
    fi
}

publish() {
    local version
    version=$(current)

    echo "Dispatching the wheel wave for dynamic-config-py ${version}."
    echo
    echo "This builds a wheel per platform and uploads with --skip-existing,"
    echo "so a version already on PyPI is a no-op rather than an error."
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

    if ! grep -q "^## \[Unreleased\]$" "${changelog}"; then
        echo "no '## [Unreleased]' heading in ${changelog}" >&2
        exit 1
    fi

    local entries
    entries=$(awk '/^## \[Unreleased\]$/ { on = 1; next } /^## \[/ { on = 0 } on && /^[-*] / { count++ } END { print count + 0 }' "${changelog}")

    if [[ ${entries} -eq 0 ]]; then
        echo "nothing under '## [Unreleased]' in ${changelog}." >&2
        echo "A release with an empty section is a release nobody can read." >&2
        exit 1
    fi

    echo "dynamic-config-py ${from} → ${to}, with ${entries} changelog entr(y|ies)."
    read -r -p "Prepare it? [y/N] " answer
    [[ ${answer,,} == y ]] || { echo "nothing changed."; exit 0; }

    # The version, in the one place it lives.
    python3 - "${manifest}" "${from}" "${to}" <<'PY'
import re
import sys

path, before, after = sys.argv[1], sys.argv[2], sys.argv[3]

with open(path, encoding="utf-8") as handle:
    text = handle.read()

package = text.index("[package]")
end = text.index("\n[", package + 1)
head, body, tail = text[:package], text[package:end], text[end:]
body, count = re.subn(rf'^version = "{re.escape(before)}"$', f'version = "{after}"',
                      body, count=1, flags=re.MULTILINE)

if count != 1:
    raise SystemExit(f"could not find version = \"{before}\" under [package]")

with open(path, "w", encoding="utf-8") as handle:
    handle.write(head + body + tail)
PY

    # The changelog, rotated the way cargo-release rotates the others.
    python3 - "${changelog}" "${to}" <<'PY'
import datetime
import sys

path, version = sys.argv[1], sys.argv[2]
today = datetime.date.today().isoformat()

with open(path, encoding="utf-8") as handle:
    text = handle.read()

heading = "## [Unreleased]"

if heading not in text:
    raise SystemExit("no Unreleased heading")

# Unbracketed, unlike the workspace changelogs: those bracket a version
# because a link definition at the bottom resolves it to a tag, and this
# package has no tag of its own — the repository's tags are workspace
# versions. A bracket with nothing behind it renders as literal brackets.
text = text.replace(
    heading,
    f"{heading}\n\n## {version} — {today}",
    1,
)

with open(path, "w", encoding="utf-8") as handle:
    handle.write(text)
PY

    git add "${manifest}" "${changelog}"
    git commit -m "release dynamic-config-py ${to}"

    echo
    echo "Prepared, not pushed. What is left:"
    echo
    echo "  1. git push origin \$(git rev-parse --abbrev-ref HEAD)"
    echo "  2. ./scripts/promote.sh          # the PR, the gates, the merge"
    echo "  3. $0 --publish                  # the wheel wave, once it is on main"
    echo
    echo "Step 3 is separate because the workspace release publishes crates"
    echo "and this publishes wheels — the same merge can carry both, and a"
    echo "Python-only release has no crates to wait for."
}

case "${1:---status}" in
    --status) status ;;
    --publish) publish ;;
    -h | --help)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *) prepare "$1" ;;
esac
