# shellcheck shell=bash
# Sourced by the step scripts beside it; never run on its own.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

note() { echo "${GITHUB_ACTIONS:+::notice::}$*"; }
die() { echo "${GITHUB_ACTIONS:+::error::}$*" >&2; exit 1; }
need() { for v; do [ -n "${!v:-}" ] || die "$v is unset: the runner environment must carry it"; done; }

# <cmd> from <dir>, inside the repo's flake shell when its root holds a flake.nix: the shell
# its own CI used, carrying the JDK, the SDKs and system libraries such as webkitgtk.
in_flake() {  # <dir> <cmd...>
  local top
  top=$(git -C "$1" rev-parse --show-toplevel) || return
  [ ! -f "$top/flake.nix" ] || set -- "$1" nix develop --accept-flake-config "$top" --command "${@:2}"
  (cd "$1" && "${@:2}")
}

# uv's own map of the workspace holding member dir $1: sets root, dir and name.
ws() {
  local meta
  dir=$(cd "$1" && pwd -P)
  meta=$(cd "$dir" && uv workspace metadata --preview-features workspace-metadata)
  root=$(jq -r .workspace_root <<<"$meta")
  name=$(jq -r --arg p "$dir" '.members[] | select(.path == $p) | .name' <<<"$meta")
  [ -n "$name" ] || die "$1 is not a member of the uv workspace at $root"
}

# mypy as the repo configures it: a mypy.ini at the workspace root, else the member's
# own [tool.mypy] over the `files` it names, else its src/, else the member.
typecheck() {  # <root> <dir> <name>
  local cwd args cache rel=${2#"$1"}
  rel=${rel#/}
  if [ -f "$1/mypy.ini" ]; then
    cwd=$1 args=(--config-file mypy.ini "${rel:-.}")
  elif grep -q '^\[tool\.mypy\]' "$2/pyproject.toml"; then
    cwd=$2 args=()
    python3 -c 'import sys, tomllib; sys.exit(0 if tomllib.load(open(sys.argv[1], "rb"))["tool"]["mypy"].get("files") else 1)' \
      "$2/pyproject.toml" || { [ -d "$2/src" ] && args=(src); } || args=(.)
  else
    note "$3: no mypy config, so no type-check" && return 0
  fi
  # A cache per member (every member has a tests.conftest, and a shared cache serves one the
  # other's), kept in the runner unit's persistent HOME so a library leg's sweep runs warm.
  cache="${HOME:-${TMPDIR:-/tmp}}/.cache/change-mypy/${GITHUB_REPOSITORY:-local}/$(cksum <<<"${2#"$1"}" | cut -d' ' -f1)"
  echo "== mypy $3 =="
  (cd "$cwd" && MYPY_CACHE_DIR=$cache uv run --no-sync mypy "${args[@]}")
}

# pytest on <path> from <cwd> in the workspace env at <root>, four workers when xdist is there.
# The path goes absolute: a conftest that changes directory at import would otherwise start
# xdist's workers where the relative path names nothing.
tests() {  # <root> <cwd> <path> [NAME=value...]
  local args=(-p no:cacheprovider)
  (cd "$1" && uv run --no-sync python -c 'import xdist' 2>/dev/null) && args+=(-n 4)
  (cd "$2" && env "${@:4}" uv run --no-sync --project "$1" python -m pytest "$(realpath "$3")" "${args[@]}")
}

# Every tracked tests/ in the repo at <top> outside the members at the given paths (the root
# member's aside), the planner's ignored dirs and any other project: sets found.
repo_tests() {  # <top> [member-path...]
  local t p files dirs
  found=()
  files=$(git -C "$1" ls-files) || return
  mapfile -t dirs < <(awk -F/ '{for (i = 1; i < NF; i++) if ($i == "tests") {
    s = $1; for (j = 2; j <= i; j++) s = s "/" $j; print s; break }}' <<<"$files" | sort -u)
  for t in "${dirs[@]}"; do
    case $t in docs/* | .claude/* | .github/* | .vscode/*) continue ;; esac
    for p in "${@:2}"; do
      if [ -n "$p" ] && [[ $t == "$p/"* ]]; then continue 2; fi
    done
    p=$(dirname "$t")
    while [ "$p" != . ]; do
      if [ -f "$1/$p/pyproject.toml" ] || [ -f "$1/$p/package.json" ] || [ -f "$1/$p/copier.yaml" ]; then
        note "$t is inside $p, a project that is not a member: not run" && continue 2
      fi
      p=$(dirname "$p")
    done
    found+=("$t")
  done
}

# Its own tests/, run from the workspace root as the repos' own runners do, under any
# build.smoke_env its server_config.json declares. The repo's root member owns every path the
# other members do not, so it runs every tests/ repo_tests finds.
suite() {  # <root> <dir> <name>
  local smoke=() found=() members=() t known top rc=0 rel=${2#"$1"}
  rel=${rel#/}
  # Explicit: uv.sh tests this with ||, which turns off set -e in here.
  top=$(cd "$(git -C "$2" rev-parse --show-toplevel)" && pwd -P) || return
  if [ "$2" = "$top" ]; then
    known=$(python3 "$here/plan.py" --repo "$top" --head HEAD --base HEAD) || return
    mapfile -t members < <(jq -r '.members[].path' <<<"$known")
    repo_tests "$top" "${members[@]}" || return
  elif [ -d "$2/tests" ]; then
    found=("${rel:+$rel/}tests")
  fi
  [ "${#found[@]}" -gt 0 ] || { note "$3: no tests/, nothing to run"; return 0; }
  [ -f "$2/server_config.json" ] &&
    mapfile -t smoke < <(jq -r '.build.smoke_env // {} | to_entries[] | "\(.key)=\(.value)"' "$2/server_config.json")
  for t in "${found[@]}"; do tests "$1" "$1" "$t" "${smoke[@]}" || rc=1; done
  return "$rc"
}

# The workspace at $1 installed whole, then the members at the given dirs (every member,
# given none) type-checked four at a time.
typecheck_all() {  # <root> [member-dir...]
  # Explicit: callers test this with ||, which turns off set -e in here.
  (cd "$1" && uv sync --frozen --all-packages --all-extras) || return
  if [ $# -gt 1 ]; then printf '%s\n' "${@:2}"; else
    (cd "$1" && uv workspace metadata --preview-features workspace-metadata) | jq -r '.members[].path'
  fi | xargs -r -P 4 -n 1 "$here/typecheck.sh"
}
