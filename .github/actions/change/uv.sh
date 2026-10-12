#!/usr/bin/env bash
# A uv member's leg: install only its closure (plus the workspace's default groups,
# which carry the test tooling), type-check it, run its own tests/.
# Usage: uv.sh <member-dir>
. "$(dirname "$0")/lib.sh"
ws "${1:-.}"
[ -f "$root/uv.lock" ] || { note "$name has no lock and no workspace: its image's smoke tests it"; exit 0; }
(cd "$root" && uv sync --frozen --package "$name" --all-extras)
rc=0
typecheck "$root" "$dir" "$name" || rc=1
suite "$root" "$dir" "$name" || rc=1
exit "$rc"
