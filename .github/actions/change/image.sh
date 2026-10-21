#!/usr/bin/env bash
# An image member's deploy: build once on the household builder, smoke it, push :<sha>,
# point :latest at it, ask Watchtower to update, then watch this host's containers of
# :latest come up healthy on that very image. A failed or interrupted go-live points :latest
# back at the digest it replaced, asks Watchtower again and watches that too. Its state
# waits in $RUNNER_TEMP until then, so `--rollback`, the deploy job's step after a cancel or
# a failure, finishes what a killed run could not.
# A container labelled household.run-from-source=true is its host's to move onto a new image:
# none is counted or watched, here or in a rollback, and Watchtower is not asked when they are
# all this host runs of it.
# The build context is the member, or the repo-relative `[tool.change] context` its
# pyproject.toml declares; the Dockerfile is always the member's own.
# Usage: image.sh <member-dir> <image-name> <sha>
#        image.sh <member-dir> <image-name> --rollback
. "$(dirname "$0")/lib.sh"
. "$(dirname "$0")/live.sh"
need CHANGE_REGISTRY CHANGE_REGISTRY_CREDENTIAL_FILE CHANGE_WATCHTOWER_URL CHANGE_WATCHTOWER_TOKEN_FILE NETRC
for f in CHANGE_REGISTRY_CREDENTIAL_FILE CHANGE_WATCHTOWER_TOKEN_FILE NETRC; do
  [ -r "${!f}" ] || die "$f (${!f}) is not readable by $(id -un)"
done
dir=$(cd "$1" && pwd) name=$2 sha=$3
ref=$CHANGE_REGISTRY/${GITHUB_REPOSITORY_OWNER:-radar-hooves}/$name
state=${RUNNER_TEMP:-${TMPDIR:-/tmp}}/change-image-$name.rollback
# A signal exits through the EXIT trap, so whatever is armed there runs.
trap 'exit 130' INT
trap 'exit 143' TERM

# No smoke script: the image must start, and pass its healthcheck, or with none stay up 20 s.
boot() {  # <image>
  local s i ok=1
  box=change-smoke-${GITHUB_RUN_ID:-local}-$$-$RANDOM
  trap 'docker rm -f "$box" >/dev/null 2>&1 || true' EXIT
  docker run -d --name "$box" "$1" >/dev/null || return
  for i in $(seq 60); do
    sleep 2
    s=$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$box")
    case $s in
      running/starting) continue ;;
      running/none) [ "$i" -ge 10 ] || continue; ok=0 ;;
      running/healthy) ok=0 ;;
    esac
    break
  done
  [ "$ok" = 0 ] || { docker logs --tail 50 "$box" 2>&1; echo "smoke: $1 is $s, not up and healthy" >&2; }
  docker rm -f "$box" >/dev/null
  trap - EXIT
  return "$ok"
}

# The password goes from its file to stdin, never through a variable or argv.
login() {
  cut -d: -f2- "$CHANGE_REGISTRY_CREDENTIAL_FILE" |
    docker login "$CHANGE_REGISTRY" -u "$(cut -d: -f1 "$CHANGE_REGISTRY_CREDENTIAL_FILE")" --password-stdin >/dev/null
}

# running()'s rows on stdin that Watchtower takes live: not a source-run one, whose host
# adopts a new image itself, holding off while a job runs inside.
by_watchtower() { awk -F'\t' '$6 != "true"'; }

# Every container of :latest here runs from source, so Watchtower is not asked at all.
host_adopts() { [ "$before" -eq 0 ] && [ "$held" -gt 0 ]; }

# A targeted update: a refused call, or any container Watchtower failed to update, fails it.
watchtower() {
  local out
  out=$({ printf 'header = "Authorization: Bearer '; tr -d '\r\n' <"$CHANGE_WATCHTOWER_TOKEN_FILE"; printf '"\n'; } |
    curl -fsS -m 600 -K - -X POST "$CHANGE_WATCHTOWER_URL/v1/update?image=$ref") || return
  echo "watchtower: $out"
  wt=$out
  jq -e '.summary.failed == 0' <<<"$out" >/dev/null
}

# The registry answers :latest with digest $1, within a minute: Watchtower reads it moments later.
serves() {
  local i d
  # An absent digest would match an absent read.
  case $1 in "" | null) die "no digest to wait for $ref:latest to answer with" ;; esac
  for i in $(seq 12); do
    d=$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$ref:latest" 2>/dev/null | jq -r .digest) || d=
    [ "$d" = "$1" ] && return 0
    sleep 5
  done
  echo "the registry still answers $ref:latest with ${d:-nothing}, not $1" >&2
  return 1
}

# Watchtower, asked again while it updates nothing and a container here still runs another
# image than $1: its digest check can read the registry a moment before :latest moved.
nudge() {  # <image id>
  local i
  for i in 1 2 3; do
    watchtower || return
    jq -e '.summary.updated > 0' <<<"$wt" >/dev/null && return 0
    running "$ref" | by_watchtower | awk -F'\t' -v w="$1" '$5 != w {stale = 1} END {exit !stale}' || return 0
    [ "$i" -eq 3 ] || { note "Watchtower updated nothing while this host runs another image; asking again"; sleep 15; }
  done
}

# As many containers of :latest here as before that Watchtower takes, each on image $1 (on any
# image but X when $1 is !X) and healthy or running; when one has no healthcheck, all
# unchanged 30 s on.
up() {  # <image id, or !id>
  local a b
  a=$(running "$ref" | by_watchtower | sort) || return
  awk -F'\t' -v n="$before" -v w="$1" '(w ~ /^!/ ? $5 == substr(w, 2) : $5 != w) || $2 !~ /^(healthy|running)$/ {bad = 1}
    END {exit bad || NR < n}' <<<"$a" || return
  grep -q $'\trunning\t' <<<"$a" || return 0
  sleep 30
  b=$(running "$ref" | by_watchtower | sort) && [ "$a" = "$b" ]
}

watch() {  # <image id, or !id>: up within 5 minutes
  local end=$((SECONDS + 300))
  until up "$1"; do
    [ "$SECONDS" -lt "$end" ] || {
      echo "after 5 minutes, $ref:latest here is (revision, health, container, restarts, image, run-from-source):" >&2
      running "$ref" >&2
      return 1
    }
    sleep 10
  done
}

# Puts :latest back at the digest it replaced, asks Watchtower, watches this host return to
# the image it ran, and says what is true.
rollback() {
  trap '' INT TERM
  trap - EXIT
  local was="$name $sha did not go live"
  if [ -z "$prev" ]; then
    rm -f "$state"
    echo "$was; there was no :latest before it, so :latest stays $sha" >&2
    return 1
  fi
  if ! { login && docker buildx imagetools create --prefer-index=false -t "$ref:latest" "$ref@$prev"; }; then
    echo "$was, and restoring :latest to $prev failed: :latest is still $sha" >&2
    return 1
  fi
  # The state stays until this host is back, so a later --rollback asks and watches again.
  serves "$prev" || true
  host_adopts || watchtower || { echo "$was; :latest is back at $prev, but Watchtower's update failed, so this host may still run $sha" >&2; return 1; }
  [ "$held" -eq 0 ] || note "$was; :latest is back at $prev, for this host to adopt in its $held source-run container(s) of it"
  if [ "$before" -eq 0 ]; then
    rm -f "$state"
    [ "$held" -gt 0 ] || note "$was; :latest is back at $prev. Nothing runs it on this host, so the restore is unverified here"
    return 0
  fi
  watch "${oldimg:-!$img}" || { echo "$was; :latest is back at $prev, but this host did not come back healthy on its previous image" >&2; return 1; }
  rm -f "$state"
  note "$was; :latest is back at $prev, and this host is healthy on ${old:-its previous image} again"
}

tree() {  # <pid>: its descendants, deepest first, then itself
  local c kids
  kids=$(cat /proc/"$1"/task/*/children 2>/dev/null || true)
  for c in $kids; do tree "$c"; done
  echo "$1"
}

if [ "$sha" = --rollback ]; then
  [ -f "$state" ] || { note "$name: nothing to roll back"; exit 0; }
  # shellcheck source=/dev/null
  . "$state"
  # The run that wrote it outlives a cancelled step: stop it and all it started first.
  if grep -qsa image.sh "/proc/$pid/cmdline"; then
    mapfile -t pids < <(tree "$pid")
    kill -KILL "${pids[@]}" 2>/dev/null || true
  fi
  if rollback; then exit 0; fi
  exit 1
fi

ctx=$dir
if [ -f "$dir/pyproject.toml" ]; then
  c=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("tool", {}).get("change", {}).get("context", ""))' "$dir/pyproject.toml")
  [ -z "$c" ] || ctx=$(cd "$(git -C "$dir" rev-parse --show-toplevel)/$c" && pwd)
fi
# Two first deploys may race to create it: the loser takes the winner's.
docker buildx inspect household >/dev/null 2>&1 ||
  docker buildx create --name household --driver docker-container --driver-opt network=host >/dev/null ||
  docker buildx inspect household >/dev/null 2>&1 ||
  die "cannot create the household builder"
docker buildx build --builder household --load --pull --build-arg "GIT_SHA=$sha" \
  --secret "id=netrc,src=$NETRC" -f "$dir/Dockerfile" -t "$ref:$sha" "$ctx"
img=$(docker image inspect --format '{{.ID}}' "$ref:$sha")
[ "$(docker image inspect --format "$revision" "$ref:$sha")" = "$sha" ] ||
  die "$ref:$sha does not carry REVISION=$sha (its Dockerfile needs ARG GIT_SHA and ENV REVISION=\${GIT_SHA}), so no watch could see it go live; nothing pushed"

probe=$dir/.github/image-smoke.sh
[ -f "$probe" ] || probe=$dir/image-smoke.sh
if [ -f "$probe" ]; then
  IMAGE=$ref:$sha bash "$probe" || die "smoke: ${probe#"$dir"/} failed on $ref:$sha; nothing pushed"
else
  boot "$ref:$sha" || die "smoke: $ref:$sha did not come up; nothing pushed"
fi

login
if out=$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$ref:latest" 2>&1); then
  prev=$(jq -r .digest <<<"$out")
else
  grep -q 'not found' <<<"$out" || die "cannot read $ref:latest: $out"
  prev=
fi
rows=$(running "$ref")
towered=$(by_watchtower <<<"$rows")
before=$(grep -c . <<<"$towered" || true)
held=$(($(grep -c . <<<"$rows" || true) - before))
live=$(sole 1 <<<"$rows") old=$(sole 1 <<<"$towered") oldimg=$(sole 5 <<<"$towered")
# A re-run of an older run must never roll production back, a source-run container's included.
[ -z "$live" ] || [ "$live" = "$sha" ] || no_rollback "$dir" "$name" "$live" "$sha" || exit 1

docker push -q "$ref:$sha"
pid=$$
declare -p ref name sha img prev old oldimg before held pid >"$state"
trap 'rollback || :; exit 1' EXIT
# Moved in the registry, not by a local tag: the live stacks share this daemon's :latest.
docker buildx imagetools create --prefer-index=false -t "$ref:latest" "$ref:$sha"
serves "$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$ref:$sha" | jq -r .digest)" ||
  die "$ref:latest did not move to $sha"
host_adopts || nudge "$img" || die "Watchtower's update of $ref failed"
[ "$held" -eq 0 ] || note "$ref:latest is $sha, for this host to adopt in its $held source-run container(s) of it"
if [ "$before" -gt 0 ]; then
  watch "$img" || die "$name $sha did not come up healthy on this host"
  note "$ref:latest is $sha, healthy on this host"
elif [ "$held" -eq 0 ]; then
  note "nothing runs $ref:latest on this host, so nothing here verifies it: :latest is $sha, unverified on this host"
fi
rm -f "$state"
trap - EXIT
