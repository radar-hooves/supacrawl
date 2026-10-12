#!/usr/bin/env bash
# Type-check one uv member against whatever its workspace has installed.
# Usage: typecheck.sh <member-dir>
. "$(dirname "$0")/lib.sh"
ws "${1:-.}"
typecheck "$root" "$dir" "$name"
