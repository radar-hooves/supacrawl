#!/usr/bin/env bash
# An internal library's sweep, its own job after the library's leg: an early warning that
# never ships, since a consumer takes the library at its own next change and its own leg
# checks it then. Its workspace dependents are type-checked against it, then the repo's
# contract suite (root tests/) runs with CHANGED_MEMBERS set to the library and them.
# Usage: library.sh <member-dir> [dependent-name...]
. "$(dirname "$0")/lib.sh"
ws "${1:-.}"
meta=$(cd "$root" && uv workspace metadata --preview-features workspace-metadata)
paths=$(jq -r '.members[] | select(.name | IN($ARGS.positional[])) | .path' --args "${@:2}" <<<"$meta")
dirs=()
[ -z "$paths" ] || mapfile -t dirs <<<"$paths"
rc=0
if [ "${#dirs[@]}" -gt 0 ]; then
  typecheck_all "$root" "${dirs[@]}" || rc=1
else
  note "$name: no workspace dependents to type-check"
  (cd "$root" && uv sync --frozen --all-packages --all-extras)
fi
top=$(git -C "$dir" rev-parse --show-toplevel) changed=("$name" "${@:2}")
[ ! -d "$top/tests" ] || tests "$root" "$top" tests "CHANGED_MEMBERS=$(IFS=,; echo "${changed[*]}")" || rc=1
exit "$rc"
