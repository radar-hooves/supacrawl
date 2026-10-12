#!/usr/bin/env bash
# A pnpm member's leg: install its closure from the lock, then run its own `check` and
# `test` scripts where it has them (lint is pre-commit's). pnpm comes from corepack, at
# the version packageManager names; corepack keeps its downloads under the persistent HOME.
# Usage: pnpm.sh <member-dir> [script...]   (default: check test)
. "$(dirname "$0")/lib.sh"
dir=$(cd "${1:-.}" && pwd)
export CI=true COREPACK_ENABLE_DOWNLOAD_PROMPT=0 COREPACK_HOME=${COREPACK_HOME:-$HOME/.cache/node/corepack}
# A shim on PATH, not `corepack pnpm`: scripts such as the kits' `test` call pnpm themselves.
mkdir -p "$COREPACK_HOME/bin" && corepack enable --install-directory "$COREPACK_HOME/bin" pnpm
export PATH=$COREPACK_HOME/bin:$PATH
root=$dir
until [ -f "$root/pnpm-lock.yaml" ]; do
  [ "$root" != / ] || die "$1: no pnpm-lock.yaml at or above it"
  root=$(dirname "$root")
done
jq -e .packageManager "$root/package.json" >/dev/null ||
  die "$root/package.json names no packageManager, so corepack would fetch whichever pnpm is newest"
filter=()
[ "$root" = "$dir" ] || filter=(--filter "$(jq -r .name "$dir/package.json")...")
(cd "$root" && pnpm install --frozen-lockfile "${filter[@]}")
rc=0
scripts=("${@:2}")
[ "${#scripts[@]}" -gt 0 ] || scripts=(check test)
for s in "${scripts[@]}"; do (cd "$dir" && pnpm run --if-present "$s") || rc=1; done
exit "$rc"
