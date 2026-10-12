#!/usr/bin/env bash
# The REVISION this host's running containers of <image-name>:latest were built from: one
# sha, or nothing when none runs or they disagree. The planner's base for an image member.
# image.sh sources it for running(), sole() and $revision.
# Usage: live.sh <image-name>

# Go template: an inspected container's or image's REVISION, and nothing else from its env.
revision='{{range .Config.Env}}{{if eq (index (split . "=") 0) "REVISION"}}{{index (split . "=") 1}}{{end}}{{end}}'

# One line per running container of image $1 (:latest), tab-separated: its REVISION, its
# health status (its state when it has no healthcheck), its id, its restart count, its image.
# `.ID`, never `.Id`: one untyped field drops docker to the raw map, where a container
# without a healthcheck has no Health key and is left out. `|| true`: one may exit mid-call.
running() {
  docker ps -q | { xargs -r docker inspect --format '{{.Config.Image}}{{"\t"}}'"$revision"'{{"\t"}}
    {{- if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}{{"\t"}}{{.ID}}{{"\t"}}{{.RestartCount}}{{"\t"}}{{.Image}}' \
    || true; } | awk -F'\t' -v r="$1" 'BEGIN {OFS = FS} $1 == r || $1 == r ":latest" {print $2, $3, $4, $5, $6}'
}

# The one distinct value in column $1 of running()'s rows on stdin; nothing when none or several.
sole() {
  local v
  v=$(cut -f"$1" | sort -u)
  [ -z "$v" ] || [ "$(wc -l <<<"$v")" -gt 1 ] || echo "$v"
}
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

. "$(dirname "$0")/lib.sh"
need CHANGE_REGISTRY
running "$CHANGE_REGISTRY/${GITHUB_REPOSITORY_OWNER:-radar-hooves}/$1" | sole 1
