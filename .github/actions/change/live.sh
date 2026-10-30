#!/usr/bin/env bash
# The REVISION this host's running containers of <image-name>:latest were built from, a tab,
# and `true` when every one runs its app from a source checkout (the stack's own label
# household.run-from-source=true): each nothing when none runs or they disagree. The planner's
# base for an image member, and whether a root image builds only for its environment.
# image.sh sources it for running(), sole() and $revision.
# Usage: live.sh <image-name>

# Go template: an inspected container's or image's REVISION, and nothing else from its env.
revision='{{range .Config.Env}}{{if eq (index (split . "=") 0) "REVISION"}}{{index (split . "=") 1}}{{end}}{{end}}'

# One line per running container of image $1 (:latest), tab-separated: its REVISION, its
# health status (its state when it has no healthcheck), its id, its restart count, its image,
# its household.run-from-source label, its name (leading slash). A one-off `docker compose run`
# container (label com.docker.compose.oneoff=True) is no service of the stack and is left out.
# `.ID`, never `.Id`: one untyped field drops docker to the raw map, where a container
# without a healthcheck has no Health key and is left out. `|| true`: one may exit mid-call.
running() {
  docker ps -q | { xargs -r docker inspect --format '{{.Config.Image}}{{"\t"}}'"$revision"'{{"\t"}}
    {{- if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}{{"\t"}}{{.ID}}{{"\t"}}{{.RestartCount}}{{"\t"}}{{.Image}}{{"\t"}}
    {{- index .Config.Labels "household.run-from-source"}}{{"\t"}}{{.Name}}{{"\t"}}
    {{- index .Config.Labels "com.docker.compose.oneoff"}}' \
    || true; } | awk -F'\t' -v r="$1" 'BEGIN {OFS = FS} ($1 == r || $1 == r ":latest") && $9 != "True" {print $2, $3, $4, $5, $6, $7, $8}'
}

# The one distinct value in column $1 of running()'s rows on stdin; nothing when none or several.
sole() {
  local v
  v=$(cut -f"$1" | sort -u)
  [ -z "$v" ] || [ "$(wc -l <<<"$v")" -gt 1 ] || echo "$v"
}
# Refuse when <live> is newer than <sha>. A live revision a full checkout lacks (a scrubbed or
# rewritten history dropped it) is no evidence of a rollback: noted, and the deploy re-baselines.
# A shallow checkout lacks it for no such reason, so it cannot show <sha> newer and refuses.
no_rollback() {  # <dir> <name> <live> <sha>
  if ! git -C "$1" cat-file -e "$3^{commit}" 2>/dev/null; then
    [ "$(git -C "$1" rev-parse --is-shallow-repository)" = false ] ||
      { echo "this host runs $2 $3, which this shallow checkout lacks, so $4 cannot be shown newer; nothing pushed" >&2; return 1; }
    echo "this host runs $2 $3, which this checkout's history lacks (rewritten): re-baselining onto $4"
    return 0
  fi
  ! git -C "$1" merge-base --is-ancestor "$4" "$3" ||
    { echo "this host runs $2 $3, which is newer than $4; refusing to roll it back, nothing pushed" >&2; return 1; }
}
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

. "$(dirname "$0")/lib.sh"
need CHANGE_REGISTRY
owner=${GITHUB_REPOSITORY_OWNER:-radar-hooves}
rows=$(running "$CHANGE_REGISTRY/${owner,,}/${1,,}")
printf '%s\t%s\n' "$(sole 1 <<<"$rows")" "$(sole 6 <<<"$rows")"
