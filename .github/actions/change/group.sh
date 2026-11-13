#!/usr/bin/env bash
# A group leg: several members run one after another on one runner (plan.sh's `groups`), each
# its own test leg and, on a shipping run, its own wait and deploy. A member that fails never
# stops the rest; the leg ends red naming every one that failed.
# Usage: group.sh   (env: MEMBERS the group's plan rows as JSON, HEAD_SHA, SHIP, CHANGE_*)
. "$(dirname "$0")/lib.sh"
need MEMBERS HEAD_SHA
ship=${SHIP:-false} failed=()
while IFS=$'\t' read -r name path toolchain test deploy now workers waits; do
  echo "::group::$name"
  rc=0
  [ "$test" != true ] || CHANGE_WORKERS=$workers "$here/$toolchain.sh" "$path" || rc=1
  if [ "$rc" = 0 ] && [ "$ship" = true ] && [ "$now" = true ] && [[ $deploy =~ ^(wheel|mcp)$ ]]; then
    # Per member, as the single leg's steps are: 26 minutes to wait, 20 to deploy.
    [ -z "$waits" ] || timeout 1560 "$here/wait.sh" "$path" $waits || rc=1
    [ "$rc" = 1 ] || timeout 1200 "$here/$deploy.sh" "$path" "$name" "$HEAD_SHA" || rc=1
  fi
  echo "::endgroup::"
  [ "$rc" = 0 ] || failed+=("$name")
done < <(jq -r '.[] | [.name, (.path | if . == "" then "." end), .toolchain, .test, .deploy, .deploy_now,
  .workers, (.waits | join(" "))] | @tsv' <<<"$MEMBERS")
for n in "${failed[@]}"; do echo "::error title=test $n failed::$n failed in ${GROUP:-its group}"; done
[ "${#failed[@]}" -eq 0 ] || die "${#failed[@]} failed: ${failed[*]}"
