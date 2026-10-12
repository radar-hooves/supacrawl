#!/usr/bin/env bash
# Repo-level legs, as the plan asks. `suites`: every tracked tests/ outside the members, the
# planner's ignored dirs and any other project, in the root workspace, with CHANGED_MEMBERS set;
# none when the repo root is itself a uv member, whose own leg runs them.
# `typecheck`: every member of every uv workspace in the repo, and each pnpm member's `check`.
# Usage: repo.sh <repo-dir> [suites] [typecheck]
. "$(dirname "$0")/lib.sh"
top=$(git -C "${1:-.}" rev-parse --show-toplevel)
known=$(python3 "$here/plan.py" --repo "$top" --head HEAD --base HEAD)
mapfile -t members < <(jq -r '.members[].path' <<<"$known")
rc=0
for want in "${@:2}"; do
  case $want in
    suites)
      owner=$(jq -r '.members[] | select(.path == "") | "\(.toolchain) \(.name)"' <<<"$known")
      [[ $owner != "uv "* ]] || { note "the repo root is the uv member ${owner#uv }: its own leg runs the tests outside the others"; continue; }
      repo_tests "$top" "${members[@]}"
      [ "${#found[@]}" -gt 0 ] || { note "no repo-level suites"; continue; }
      if [ ! -f "$top/uv.lock" ]; then
        [ -n "$owner" ] || die "repo suites ${found[*]} need a uv workspace at the repo root"
        # thoth: its root pnpm member's tests/e2e and src-tauri/tests are no pytest suites.
        note "${found[*]} lie in the ${owner% *} member ${owner#* }, with no uv workspace at the root: not run here" && continue
      fi
      (cd "$top" && uv sync --frozen --all-packages --all-extras)
      for t in "${found[@]}"; do tests "$top" "$top" "$t" || rc=1; done ;;
    typecheck)
      files=$(git -C "$top" ls-files 'uv.lock' '*/uv.lock')
      mapfile -t locks < <(grep . <<<"$files" || true)
      for lock in "${locks[@]}"; do typecheck_all "$top/$(dirname "$lock")" || rc=1; done
      mapfile -t pnpm < <(jq -r '.members[] | select(.toolchain == "pnpm") | .path' <<<"$known")
      for p in "${pnpm[@]}"; do "$here/pnpm.sh" "$top/$p" check || rc=1; done ;;
    *) die "repo.sh: unknown leg '$want' (suites, typecheck)" ;;
  esac
done
exit "$rc"
