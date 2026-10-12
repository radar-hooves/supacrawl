#!/usr/bin/env bash
# A Gradle member's leg: its project's `check` (Android's includes Lint), by the build's own
# wrapper. A repo's exclusions and setup belong in its own build, never here.
# Usage: gradle.sh <member-dir>
. "$(dirname "$0")/lib.sh"
dir=$(cd "${1:-.}" && pwd)
up() {  # the nearest dir at or above $1 holding a Gradle settings file, if any
  local d=$1
  until [ -f "$d/settings.gradle.kts" ] || [ -f "$d/settings.gradle" ]; do
    [ "$d" != / ] || return 0
    d=$(dirname "$d")
  done
  echo "$d"
}
root=$(up "$dir")
[ -n "$root" ] || die "$1: no settings.gradle(.kts) at or above it"
task=("$(tr / : <<<"${dir#"$root"}"):check")
# A member holding its own settings under another build's is an included build. Its `check`
# in the composite (:<build>:check) reaches only its root project, so it runs as a build of
# its own, by the parent's wrapper.
if [ "$root" = "$dir" ] && parent=$(up "$(dirname "$dir")") && [ -n "$parent" ]; then
  root=$parent task=(-p "${dir#"$parent"/}" check)
fi
in_flake "$root" ./gradlew --no-daemon --stacktrace "${task[@]}"
