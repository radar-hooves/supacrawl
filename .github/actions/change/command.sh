#!/usr/bin/env bash
# A declared-command member's leg: the `test` command of <dir>/.github/change.toml, run from
# <dir> inside the repo's flake shell. It builds, ships and deploys nothing.
# Usage: command.sh <repo-dir>
. "$(dirname "$0")/lib.sh"
dir=$(cd "${1:-.}" && pwd)
cmd=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["test"])' "$dir/.github/change.toml") ||
  die "$dir/.github/change.toml declares no test command"
in_flake "$dir" bash -euo pipefail -c "$cmd"
