#!/usr/bin/env bash
# Resolve head and bases for the event, run plan.py, and hand its legs to the workflow.
# A pull request diffs from its merge-base. A push or dispatch diffs from the newest run of
# this workflow on main whose plan passed and whose head is an ancestor of this one, and
# rolls every leg that failed there, since it never shipped; an image diffs from the
# revision running live. A root image whose containers carry household.run-from-source=true
# serves code its host fast-forwards from a checkout, so it builds only when its environment
# moved, and waits on no test. A fork's pull request plans nothing: its code never runs here.
# A tag plans nothing either: it releases through the repo's own release workflow.
# Usage: plan.sh <repo-dir> [head [base]]   (outputs on stdout, summary on stderr by hand)
. "$(dirname "$0")/lib.sh"
repo=$(cd "${1:-.}" && pwd) head=${2:-} base=${3:-}
ev=${GITHUB_EVENT_NAME:-} event=${GITHUB_EVENT_PATH:-/dev/null} roll=${ROLL:-}
carried=() bases=() force='' history=''
none='{"members":[],"artefacts":[],"libraries":[],"repo_suites":false,"typecheck_all":false,"changed_members":"","reasons":{}}'

# The newest usable run of this workflow on main: sets base and carried (its failed legs),
# and history when main has any completed run at all.
walk() {
  local runs id sha kind end jobs
  runs=$(gh api "repos/$slug/actions/workflows/change.yaml/runs?branch=main&status=completed&per_page=20" \
    --jq '.workflow_runs[] | "\(.id) \(.head_sha) \(.event) \(.conclusion)"') ||
    die "cannot list $slug's change.yaml runs: the caller must grant actions: read (by hand, pass a base)"
  [ -z "$runs" ] || history=1
  while read -r id sha kind end; do
    [ -n "$id" ] && [ "$kind" != pull_request ] && [ "$end" != cancelled ] || continue
    git -C "$repo" merge-base --is-ancestor "$sha" "$head" 2>/dev/null || continue
    jobs=$(gh api --paginate "repos/$slug/actions/runs/$id/jobs?per_page=100" </dev/null \
      --jq '.jobs[] | "\(.conclusion)\t\(.name | sub("^[^ ]+ / "; ""))"') || die "cannot read run $id's jobs"
    grep -qx $'success\tplan' <<<"$jobs" || continue
    base=$sha
    mapfile -t carried < <(awk -F'\t' '$1 ~ /^(failure|cancelled|timed_out)$/ {print $2}' <<<"$jobs")
    note "base: run $id at ${sha:0:12}${carried[*]:+; carried forward: ${carried[*]}}"
    return
  done <<<"$runs"
  [ -z "$history" ] || note "no usable run among main's last 20: no base, so every member tests and none ships"
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
    if [ -n "$base" ]; then
      base=$(git -C "$repo" rev-parse "$base")
    else
      slug=${GITHUB_REPOSITORY:-$(cd "$repo" && gh repo view --json nameWithOwner -q .nameWithOwner)}
      walk
      [ -n "$base$history" ] || base=$(git -C "$repo" rev-parse -q --verify "$(jq -r '.before // empty' "$event")^{commit}" || true)
    fi
    # Names only, so failed legs map to members and images to their live revisions.
    known=$(python3 "$here/plan.py" --repo "$repo" --head "$head" --base "$head")
    if [ "${#carried[@]}" -gt 0 ]; then
      [[ " ${carried[*]} " != *" repo "* ]] || force=1
      more=$(jq -r '(.artefacts | map({key: .image, value: .members}) | from_entries) as $a
        | [.members[].name] as $m | [$ARGS.positional[] | capture("^(test|library|deploy) (?<n>.+)$").n
        | if IN($m[]) then . else ($a[.] // [])[] end] | unique | join(",")' --args "${carried[@]}" <<<"$known")
      [ "$roll" = all ] || roll=$(tr -s , <<<"$roll,$more" | sed 's/^,//; s/,$//')
    fi
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
{
  echo "plan=$plan"
  echo "head=$head"
  echo "members=$(jq -c --argjson s "$ship" '.libraries as $l | [.members[] | select((.test or (.deploy_now and $s)) and (.name | IN($l[]) | not))]' <<<"$plan")"
  echo "libraries=$(jq -c '[.libraries[] as $n | .members[] | select(.name == $n)]' <<<"$plan")"
  echo "deploys=$deploys"
  echo "builds=$(jq -c '[.[] | select(.split) | .phase = "build"]' <<<"$deploys")"
  echo "repo=$(if [ -n "$force" ]; then echo suites typecheck; else
    jq -r '[(.repo_suites | select(.) | "suites"), (.typecheck_all | select(.) | "typecheck")] | join(" ")' <<<"$plan"; fi)"
  echo "changed=$(jq -r .changed_members <<<"$plan" | tr -cd 'A-Za-z0-9._,-')"
  echo "ship=$ship"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"
{
  echo "### change: \`${base:0:12}\` → \`${head:0:12}\`"
  jq -r 'if .reasons == {} then "Nothing changed that runs anything."
    else .reasons | to_entries[] | "- **\(.key)**: \(.value | join(", "))" end' <<<"$plan"
} >>"${GITHUB_STEP_SUMMARY:-/dev/stderr}"
