#!/usr/bin/env bash
# A Cargo member's leg: `cargo check` over every target, then `cargo test`, both held to the
# lock and over the whole workspace at a workspace root (fmt and clippy are pre-commit's).
# cargo's default home, ~/.cargo, keeps the crate cache in the persistent HOME.
# Usage: cargo.sh <member-dir>
. "$(dirname "$0")/lib.sh"
dir=$(cd "${1:-.}" && pwd)
all=()
# A workspace root that is also a package otherwise builds only itself.
if grep -q '^\[workspace\]' "$dir/Cargo.toml"; then all=(--workspace); fi
in_flake "$dir" cargo check --locked --all-targets "${all[@]}" &&
  in_flake "$dir" cargo test --locked "${all[@]}"
