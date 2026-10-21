#!/usr/bin/env bash
# Wait for pass markers in this run: a marker is set once its member's own leg finishes its
# `passed` step in any attempt of this run, read from its jobs, never an artifact (vendor
# storage the quota can close). Fails once every attempt of that leg has finished without it,
# or after 25 min.
# Usage: wait.sh <dir> <marker...>   (dir is unused; Actions only, with GH_TOKEN able to read
# this run's jobs)
. "$(dirname "$0")/lib.sh"
need GITHUB_REPOSITORY GITHUB_RUN_ID GH_TOKEN
api=repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID
deadline=$((SECONDS + 1500))
# Each leg's state under its marker: passed, failed (finished without passing) or running.
state() {
  gh api --paginate "$api/jobs?per_page=100&filter=all" --jq '.jobs[] | {name: (.name | sub("^[^ ]+ / "; "")),
      done: (.status == "completed"), passed: any(.steps[]?; .name == "passed" and .conclusion == "success")}' |
    jq -r --arg m "$1" 'select(.name | test("^(test|library) "))
      | select((.name | capture("^(test|library) (?<n>.+)$").n | "passed-" + gsub("[^A-Za-z0-9._-]"; "_")) == $m)
      | if .passed then "passed" elif .done then "failed" else "running" end' | sort -u
}
for m in "${@:2}"; do
  while s=$(state "$m"); ! grep -qx passed <<<"$s"; do
    if [ "$s" = failed ]; then die "${m#passed-} finished its leg without passing, so this does not ship"; fi
    [ "$SECONDS" -lt "$deadline" ] || die "gave up after 25 min waiting for ${m#passed-}'s leg"
    sleep 20
  done
  note "${m#passed-} passed its leg"
done
