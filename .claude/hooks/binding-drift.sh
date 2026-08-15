#!/usr/bin/env bash
# Names the files a change has to travel to, the moment it is made.
#
# Advisory by design: it exits 0 and never blocks a tool call.
set -euo pipefail

input=$(cat)
path=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' 2>/dev/null || true)

[ -z "$path" ] && exit 0

case "$path" in
  */dynamic-config-python/src/config.rs)
    cat <<'NOTE'
The compiled surface moved. A method added or changed there has to reach:
  · python/dynamic_config/__init__.py   the facade wrapper, with a docstring
  · python/dynamic_config/_core.pyi     or mypy --strict stops seeing through
  · book/src/reference.md               async twins share a row
  · tests/                              the behaviour, not the call
  · dynamic-config-python/CHANGELOG.md  under Unreleased
NOTE
    ;;
  */dynamic-config-python/python/dynamic_config/__init__.py)
    cat <<'NOTE'
The facade moved. Check that _core.pyi and book/src/reference.md followed,
and that every public definition still carries a docstring — this package
is fully documented and `help()` is its manual.
NOTE
    ;;
  */pyproject.toml)
    cat <<'NOTE'
Both wheels version together, and the remote one declares a floor on the
base one. `scripts/release-python.sh --check` is what proves the three
still agree.
NOTE
    ;;
esac

exit 0
