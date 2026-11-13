#!/usr/bin/env bash
# Resolve head and bases for the event, run plan.py, and hand its legs to the workflow.
# A pull request diffs from its merge-base. A push or dispatch diffs from the newest run of
# this workflow on main that went green and whose head is an ancestor of this one, so code a
# red run left unshipped ships with the next green one; an image diffs from the
# revision running live. A root image whose containers carry household.run-from-source=true
# serves code its host fast-forwards from a checkout, so it builds only when its environment
# moved, and waits on no test. A fork's pull request plans nothing: its code never runs here.
# A tag plans nothing either: it releases through the repo's own release workflow.
# A push whose tree a green run of this workflow already tested (its branch, before it
# landed) skips those member test legs and ships on that run's tests. A green group leg
# names its members in its `passed: ...` step, so grouped members skip too.
# More than CHANGE_GROUPS members of one runner class share that many group legs (group.sh),
# Gradle's past CHANGE_GRADLE_GROUPS, its pool's size; a library, an image's member or a
# declared-runner member keeps its own leg.
# Usage: plan.sh <repo-dir> [head [base]]   (outputs on stdout, summary on stderr by hand)
. "$(dirname "$0")/lib.sh"
repo=$(cd "${1:-.}" && pwd) head=${2:-} base=${3:-}
ev=${GITHUB_EVENT_NAME:-} event=${GITHUB_EVENT_PATH:-/dev/null} roll=${ROLL:-}
bases=() history=''
none='{"members":[],"artefacts":[],"libraries":[],"repo_suites":false,"typecheck_all":false,"changed_members":"","reasons":{}}'

# The newest run of this workflow on main that went green and whose head is an ancestor of
# this one: sets base, and history when main has any completed run at all. A run that did not
# go green never shipped what it planned, whichever leg failed, so it is no base: the next
# green run diffs from the last commit that shipped and takes the failed run's code with it.
walk() {
  local runs id sha kind end
  runs=$(gh api "repos/$slug/actions/workflows/change.yaml/runs?branch=main&status=completed&per_page=20" \
    --jq '.workflow_runs[] | "\(.id) \(.head_sha) \(.event) \(.conclusion)"') ||
    die "cannot list $slug's change.yaml runs: the caller must grant actions: read (by hand, pass a base)"
  [ -z "$runs" ] || history=1
  while read -r id sha kind end; do
    [ -n "$id" ] && [ "$kind" != pull_request ] && [ "$end" = success ] || continue
    git -C "$repo" merge-base --is-ancestor "$sha" "$head" 2>/dev/null || continue
    base=$sha
    note "base: run $id at ${sha:0:12}"
    return
  done <<<"$runs"
  [ -z "$history" ] || note "no green run among main's last 20: no base, so every member tests and none ships"
}

# The test legs a green run of this workflow passed on this push's exact tree: none when no
# run among the last 20 green ones carried it.
tested() {
  local tree id sha
  tree=$(git -C "$repo" rev-parse "$head^{tree}")
  gh api "repos/$slug/actions/workflows/change.yaml/runs?status=success&per_page=20" \
    --jq '.workflow_runs[] | "\(.id) \(.head_sha)"' | while read -r id sha; do
    [ "$id" != "${GITHUB_RUN_ID:-}" ] || continue
    [ "$(gh api "repos/$slug/commits/$sha" --jq .commit.tree.sha 2>/dev/null)" = "$tree" ] || continue
    note "tree ${tree:0:12} passed run $id at ${sha:0:12}: its member tests are not run again" >&2
    gh api --paginate "repos/$slug/actions/runs/$id/jobs?per_page=100" </dev/null \
      --jq '.jobs[] | select(.conclusion == "success") | (.name | sub("^[^ ]+ / "; "")),
        (.steps[]?.name | select(startswith("passed: ")) | ltrimstr("passed: ") | split(" ")[] | "test " + .)'
    return
  done
}

fork=$(jq -r '.pull_request.head.repo.full_name // empty' "$event")
if [ "${GITHUB_REF_TYPE:-}" = tag ]; then
  note "a tag releases through the repo's own release workflow, for an outside consumer: nothing is planned"
  plan=$none
elif [ -n "$fork" ] && [ "$fork" != "${GITHUB_REPOSITORY:-}" ]; then
  note "pull request from the fork $fork: its code never runs on this runner, so nothing is planned"
  plan=$none
else
  if [ "$ev" = pull_request ]; then
    head=$(jq -r .pull_request.head.sha "$event")
    base=$(git -C "$repo" merge-base "$(jq -r .pull_request.base.sha "$event")" "$head")
  else
    head=$(git -C "$repo" rev-parse "${head:-HEAD}")
    slug=${GITHUB_REPOSITORY:-$(cd "$repo" && gh repo view --json nameWithOwner -q .nameWithOwner)}
    if [ -n "$base" ]; then
      base=$(git -C "$repo" rev-parse "$base")
    else
      walk
      [ -n "$base$history" ] || base=$(git -C "$repo" rev-parse -q --verify "$(jq -r '.before // empty' "$event")^{commit}" || true)
    fi
    # Names only, so images map to their live revisions.
    known=$(python3 "$here/plan.py" --repo "$repo" --head "$head" --base "$head")
    roots=$(jq -r '.artefacts[].image' <<<"$known")
    mapfile -t images < <(jq -r '.artefacts[].image, (.members[] | select(.deploy == "image") | .name)' <<<"$known")
    for img in "${images[@]}"; do
      live=$("$here/live.sh" "$img")
      rev=${live%%$'\t'*}
      if [ -n "$rev" ] && git -C "$repo" rev-parse -q --verify "$rev^{commit}" >/dev/null; then
        bases+=(--base "$img=$rev")
      fi
      if [ "${live#*$'\t'}" != true ]; then :
      elif grep -qxF "$img" <<<"$roots"; then bases+=(--source "$img")
      else note "$img runs from source, but only a root image builds as one: it builds as before"
      fi
    done
  fi
  plan=$(python3 "$here/plan.py" --repo "$repo" --head "$head" ${base:+--base "$base"} "${bases[@]}" --roll "$roll")
  if [ "$ev" != pull_request ] && [ -z "$roll" ]; then
    mapfile -t passed < <(tested)
    # A library keeps its leg: its pass marker in this run is what its dependents ship on.
    [ "${#passed[@]}" -eq 0 ] || plan=$(jq -c '.libraries as $l | .members |= map(
      if .test and (.name | IN($l[]) | not) and (("test " + .name) | IN($ARGS.positional[]))
      then .test = false else . end)' --args "${passed[@]}" <<<"$plan")
  fi
fi

# A marker per member: its own leg's `passed` step, which wait.sh reads. A member's `waits`
# are the markers of this run's libraries it ships with, transitively (`carried_by`): it ships
# none of them red. Libraries run dependencies first, since each runs alone and may wait on another.
plan=$(jq -c 'def marker: "passed-" + gsub("[^A-Za-z0-9._-]"; "_");
  (.members | map({key: .name, value: .carried_by}) | from_entries) as $deps
  | def below: . as $s | ($s + [$s[] | $deps[.][]?]) | unique | if . == $s then . else below end;
  (.libraries | map({key: ., value: (([.] | below) - [.])}) | from_entries) as $under
  | .members |= map(.name as $n | . + {marker: ($n | marker),
      waits: [$under | to_entries[] | select(any(.value[]; . == $n)) | .key | marker]})
  | .libraries |= sort_by(. as $l | [$under[] | select(any(.[]; . == $l))] | length)' <<<"$plan")
ship=false
[ "${GITHUB_REF:-}" = refs/heads/main ] && [[ $ev =~ ^(push|workflow_dispatch)$ ]] && ship=true
# Images build after the tests, off the test legs: BuildKit's memory is outside every runner's cap.
# One whose build runner differs from its deploy runner is `split`: it builds in its own job.
deploys=$(jq -c --argjson s "$ship" '(.members | map({key: .name, value: .}) | from_entries) as $by
  | [.members[] | select(.deploy_now and .deploy == "image")
     | {kind: "image", path: (.path | if . == "" then "." end), name, markers: ([.marker] + .waits), build_on, deploy_on}]
  + [.artefacts[] | select(.build) | {kind: "image", path: ".", name: .image,
     markers: (if .source then [] else [.members[] | $by[.] | (select(.test or (.deploy_now and $s)) | .marker), .waits[]] | unique end),
     build_on, deploy_on}]
  | map(. + {split: ((.build_on | sort) != (.deploy_on | sort))} | . + {phase: (if .split then "release" else "" end)})' <<<"$plan")
groups=${CHANGE_GROUPS:-6} gradle=${CHANGE_GRADLE_GROUPS:-2}
split=$(jq -c --argjson s "$ship" --argjson n "$groups" --argjson gn "$gradle" 'def rel: .deploy_now and $s and (.deploy | IN("wheel", "mcp"));
  def class: if .toolchain == "gradle" then "gradle-" elif rel then "release-" else "" end;
  .libraries as $l | [.artefacts[].members[]] as $held
  | [.members[] | select((.test or (.deploy_now and $s)) and (.name | IN($l[]) | not))] as $run
  | [$run[] | select((has("test_on") | not) and .deploy != "image" and (.name | IN($held[]) | not))] as $el
  | [("", "release-", "gradle-") as $c | (if $c == "gradle-" then $gn else $n end) as $k
     | [$el[] | select(class == $c)] | select(length > $k) | to_entries | group_by(.key % $k)
     | to_entries[] | {name: ($c + "group-" + (.key + 1 | tostring)), release: ($c == "release-"),
       gradle: ($c == "gradle-"), members: (.value | map(.value))}
     | . + {names: [.members[].name], deep: ($s and any(.members[]; .deploy_now))}] as $g
  | {groups: $g, members: ($run - [$g[].members[]])}' <<<"$plan")
{
  echo "plan=$plan"
  echo "head=$head"
  echo "members=$(jq -c .members <<<"$split")"
  echo "groups=$(jq -c .groups <<<"$split")"
  echo "libraries=$(jq -c '[.libraries[] as $n | .members[] | select(.name == $n)]' <<<"$plan")"
  echo "deploys=$deploys"
  echo "builds=$(jq -c '[.[] | select(.split) | .phase = "build"]' <<<"$deploys")"
  echo "repo=$(jq -r '[(.repo_suites | select(.) | "suites"), (.typecheck_all | select(.) | "typecheck")] | join(" ")' <<<"$plan")"
  echo "changed=$(jq -r .changed_members <<<"$plan" | tr -cd 'A-Za-z0-9._,-')"
  echo "ship=$ship"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"
{
  echo "### change: \`${base:0:12}\` → \`${head:0:12}\`"
  jq -r 'if .reasons == {} then "Nothing changed that runs anything."
    else .reasons | to_entries[] | "- **\(.key)**: \(.value | join(", "))" end' <<<"$plan"
} >>"${GITHUB_STEP_SUMMARY:-/dev/stderr}"
