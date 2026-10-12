#!/usr/bin/env bash
# Wait for pass markers in this run: each is the artifact a member's own leg uploads when it
# passes. Fails once the leg that would upload one has finished without it, or after 25 min.
# Usage: wait.sh <dir> <marker...>   (dir is unused; Actions only, with GH_TOKEN able to read
# this run's jobs and artifacts)
. "$(dirname "$0")/lib.sh"
need GITHUB_REPOSITORY GITHUB_RUN_ID GH_TOKEN
api=repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID
deadline=$((SECONDS + 1500))
have() { local names; names=$(gh api --paginate "$api/artifacts?per_page=100" --jq '.artifacts[].name') && grep -qxF "$1" <<<"$names"; }
for m in "${@:2}"; do
  until have "$m"; do
    finished=$(gh api --paginate "$api/jobs?per_page=100" --jq '.jobs[] | select(.status == "completed") | .name' |
      jq -Rr 'sub("^[^ ]+ / "; "") | capture("^(test|library) (?<n>.+)$").n | "passed-" + gsub("[^A-Za-z0-9._-]"; "_")')
    if grep -qxF "$m" <<<"$finished"; then
      have "$m" || die "${m#passed-} finished its leg without passing, so this does not ship"
      break
    fi
    [ "$SECONDS" -lt "$deadline" ] || die "gave up after 25 min waiting for ${m#passed-}'s leg"
    sleep 20
  done
  note "${m#passed-} passed its leg"
done
